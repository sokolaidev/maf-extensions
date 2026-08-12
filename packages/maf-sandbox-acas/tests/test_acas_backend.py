"""Offline tests for the ACA Sandboxes backend.

No live sandbox group and no host application: the group client is replaced by a fake, and
the disk-image tests build the **real** SDK dataclasses so the shape they assert is the
SDK's rather than one the code and the fake happen to agree on.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath

import pytest
from maf_sandbox import Capability, Egress, Isolation, SandboxBackend, SandboxKey, SandboxRouter

from maf_sandbox_acas import (
    AcasEntryPayloadIncomplete,
    AcasSandboxBackend,
    AcasSandboxConfig,
    disk_image_base,
    resolve_disk_image_id,
)

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
        self.resumed = False

    async def begin_delete(self) -> None:
        self.deleted = True

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
        assert AcasSandboxBackend(_config()).egress == Egress.ALLOWLIST

    def test_declares_exec_files_in_and_the_whole_pull_surface(self):
        """Declares only what it implements today — no ATTACHED_IDENTITY and no SNAPSHOT."""
        assert AcasSandboxBackend(_config()).capabilities == frozenset(
            {
                Capability.EXEC,
                Capability.FILES_IN,
                Capability.FILES_OUT,
                Capability.FILES_LIST,
            }
        )

    def test_is_the_only_backend_that_can_declare_files_list(self):
        """Native enumeration is the split's own test — name the backend that lacks it."""
        assert Capability.FILES_LIST in AcasSandboxBackend(_config()).capabilities

    def test_declares_transfer_ceilings_that_admit_a_spec_saying_nothing(self):
        """The spec-side default must stay within them, or every existing spec fails at attach."""
        from maf_sandbox import DEFAULT_TRANSFER_LIMITS

        limits = AcasSandboxBackend(_config()).limits
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

    def test_a_spec_asking_above_the_transfer_ceiling_is_refused(self):
        from maf_sandbox import SandboxSpec, SandboxTransferLimitsNotPermitted, TransferLimits

        backend = AcasSandboxBackend(_config())
        ceiling = backend.limits.files_out
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
        assert AcasSandboxBackend(_config()).name == "acas"


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
        """The registry is a fast path; the service is the source of truth."""
        client = _FakeGroupClient(sandboxes=[_FakeSandbox("sbx-remote")])
        backend = _backend_with(client)

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 1
        assert client.deleted == ["sbx-remote"]
        assert client.last_labels == {"scope": "scope-a", "thread": "thread-1"}

    def test_unions_the_registry_with_the_service_listing(self):
        client = _FakeGroupClient(sandboxes=[_FakeSandbox("sbx-remote")])
        backend = _backend_with(client)
        backend._registry[("scope-a", "thread-1", "devops-engineer", "bicep")] = "sbx-local"

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 2
        assert sorted(client.deleted) == ["sbx-local", "sbx-remote"]

    def test_does_not_delete_another_scopes_sandbox(self):
        client = _FakeGroupClient()
        backend = _backend_with(client)
        backend._registry[("scope-b", "thread-1", "devops-engineer", "bicep")] = "sbx-other"

        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 0
        assert client.deleted == []
        assert ("scope-b", "thread-1", "devops-engineer", "bicep") in backend._registry

    def test_registry_entries_are_dropped_even_when_the_delete_fails(self):
        """A stale entry is worse than none — the next acquire would try to resume it."""
        backend = _backend_with(_ExplodingGroupClient())
        backend._registry[("scope-a", "thread-1", "devops-engineer", "bicep")] = "sbx-local"

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
        from maf_sandbox_acas._backend import _AcasSandbox

        class _RecordingClient:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            async def write_file(self, path, content, **kwargs):
                self.calls.append((path, content, kwargs))

        client = _RecordingClient()
        asyncio.run(
            _AcasSandbox(client, 30.0).write_file("/work/infra/main.bicep", "param x string")
        )

        assert client.calls == [("/work/infra/main.bicep", "param x string", {"create_dirs": True})]


class TestExecArgv:
    """`_AcasSandbox.exec` accepts a sequence and quotes it before the SDK's string-only exec.

    The SDK's ``exec`` takes one string; a caller handing this an argv sequence must be able
    to trust that no element — however it is shaped — can be re-interpreted as more than one
    token or a second command once the sandbox's shell sees it.  ``shlex.split`` of what was
    actually sent to the SDK recovering the exact original argv is that proof.
    """

    class _RecordingClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def exec(self, command: str, *, working_directory: str):
            self.calls.append(command)

            class _Result:
                stdout = ""
                stderr = ""
                exit_code = 0

            return _Result()

    def test_a_string_command_passes_through_unchanged(self):
        from maf_sandbox_acas._backend import _AcasSandbox

        client = self._RecordingClient()
        asyncio.run(
            _AcasSandbox(client, 30.0).exec("echo hi", working_directory="/work", timeout=5)
        )
        assert client.calls == ["echo hi"]

    def test_a_sequence_is_quoted_with_shlex_join(self):
        import shlex

        from maf_sandbox_acas._backend import _AcasSandbox

        client = self._RecordingClient()
        argv = ["echo", "a; rm -rf /", "$(id)", "`id`", "it's mine", 'say "hi"']
        asyncio.run(_AcasSandbox(client, 30.0).exec(argv, working_directory="/work", timeout=5))

        assert client.calls == [shlex.join(argv)]
        # Round-tripping through shlex.split recovers the exact argv — proof the quoted
        # form cannot be re-interpreted as more than one token per element, and cannot
        # break out into a second shell command.
        assert shlex.split(client.calls[0]) == argv

    def test_a_bare_space_separated_argv_stays_one_command(self):
        import shlex

        from maf_sandbox_acas._backend import _AcasSandbox

        client = self._RecordingClient()
        argv = ["bicep", "build", "/acas/work/r1/main.bicep", "--diagnostics-format", "sarif"]
        asyncio.run(_AcasSandbox(client, 30.0).exec(argv, working_directory="/work", timeout=5))

        assert shlex.split(client.calls[0]) == argv


class TestDispose:
    def test_deletes_the_keyed_sandbox_and_forgets_it(self):
        client = _FakeGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._registry[(key.scope, key.thread_id, key.agent_dir, "bicep")] = "sbx-1"

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
        backend._registry[(key.scope, key.thread_id, key.agent_dir, "bicep")] = "sbx-warm"

        from maf_sandbox import SandboxSpec

        with caplog.at_level(logging.INFO, logger="maf_sandbox_acas"):
            asyncio.run(backend.acquire(key, SandboxSpec(kind="bicep", image="img:1")))

        assert any("sandbox reused" in r.getMessage() for r in caplog.records), caplog.text
        assert any("sbx-warm" in r.getMessage() for r in caplog.records)

    def test_release_is_logged(self, caplog):
        client = _FakeGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._registry[(key.scope, key.thread_id, key.agent_dir, "bicep")] = "sbx-1"

        with caplog.at_level(logging.INFO, logger="maf_sandbox_acas"):
            asyncio.run(backend.dispose(key))

        assert any("sandbox released" in r.getMessage() for r in caplog.records), caplog.text

    def test_a_scope_purge_names_each_sandbox_it_deletes(self, caplog):
        client = _FakeGroupClient(sandboxes=[_FakeSandbox("sbx-a"), _FakeSandbox("sbx-b")])
        backend = _backend_with(client)

        with caplog.at_level(logging.INFO, logger="maf_sandbox_acas"):
            count = asyncio.run(backend.dispose_scope("scope-a", "thread-1"))

        assert count == 2
        released = [r for r in caplog.records if "sandbox released" in r.getMessage()]
        assert len(released) == 2, caplog.text


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


class _CreatedSandbox:
    def __init__(self, sandbox_id: str) -> None:
        self.sandbox_id = sandbox_id

    async def set_lifecycle_policy(self, policy) -> None:
        await asyncio.sleep(0)


def _spec():
    from maf_sandbox import SandboxSpec

    return SandboxSpec(kind="bicep", image_id="pinned-id")


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
        assert backend._registry == {("scope-a", "thread-1", "devops-engineer", "bicep"): "sbx-1"}

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
        """An `asyncio.Lock` binds to the loop that first had to wait on it.

        This backend is reachable from more than one loop — the same reason its group clients
        are cached per loop — so a lock kept per key alone raises ``RuntimeError`` the first
        time two calls contend on a second loop. `SandboxToolSession.acquire` reports that as
        "sandbox unavailable", so the run degrades to T0 with nothing naming the cause.
        """
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
        assert ("scope-a", "thread-1", "devops-engineer", "bicep") in backend._registry
        assert ("scope-a", "thread-1", "devops-engineer", "codeact") in backend._registry

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
        backend._registry[(key.scope, key.thread_id, key.agent_dir, "bicep")] = "sbx-b"
        backend._registry[(key.scope, key.thread_id, key.agent_dir, "codeact")] = "sbx-c"

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

        class _CreatedSandbox:
            def __init__(self, sandbox_id: str) -> None:
                self.sandbox_id = sandbox_id

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
        backend._registry[(key.scope, key.thread_id, key.agent_dir, "bicep")] = "sbx-warm"

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
        backend._registry[(key.scope, key.thread_id, key.agent_dir, "bicep")] = "sbx-1"

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

    def test_the_model_facing_surface_is_unaffected(self):
        """This is a log-content-only change: `error_detail` never reaches a tool result.

        Nothing in this backend returns `error_detail`'s output to a caller — it is only
        ever handed to `logger.warning`/`logger.info`. This guards that boundary staying
        true rather than re-deriving it by reading the source on every review.
        """
        import inspect

        from maf_sandbox_acas import _backend

        source = inspect.getsource(_backend)
        assert source.count("error_detail(") == 3, (
            "expected exactly the resume, delete and list call sites to adopt error_detail; "
            "a new call site should extend this test rather than silently changing the count"
        )


# ---------------------------------------------------------------------------
# Egress policy — built from the spec, not from configuration
# ---------------------------------------------------------------------------


class TestEgressPolicy:
    def test_denies_by_default_and_allows_only_the_named_hosts(self):
        """Patterns pass through verbatim — including wildcards, which the Bicep spec's
        `*.data.mcr.microsoft.com` (MCR's blob endpoint) depends on."""
        from maf_sandbox import SandboxSpec

        backend = AcasSandboxBackend(_config())
        policy = backend._egress_policy(
            SandboxSpec(kind="t", egress_allow=("mcr.microsoft.com", "*.data.mcr.microsoft.com"))
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

_WORK_DIR = "/work"
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
    "path": "/work/real.txt",
    "size": 12,
    "mode": 420,
    "isDir": False,
    "isSymlink": False,
    "modifiedTime": 1786404028,
}
_LIVE_SYMLINK = {
    "name": "link-out.txt",
    "path": "/work/link-out.txt",
    "size": 13,
    "mode": 511,
    "isDir": False,
    "isSymlink": True,
    "symlinkTarget": "/etc/hostname",
    "modifiedTime": 1786404028,
}
_LIVE_DIRECTORY = {
    "name": "sub",
    "path": "/work/sub",
    "size": 4096,
    "mode": 493,
    "isDir": True,
    "isSymlink": False,
    "modifiedTime": 1786404028,
}
_LIVE_NESTED = {
    "name": "child.txt",
    "path": "/work/sub/child.txt",
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
#: `ln -sfn /etc /work/out` inside the guest. `size` is 4 — the length of `/etc`, the target
#: *string*. The link itself types as OTHER; a path *through* it carries nothing that says so.
_LIVE_SYMLINK_DIR = {
    "name": "out",
    "path": "/work/out",
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
    _WORK_DIR: _LIVE_WORK_DIR,
    "/work/real.txt": _LIVE_REGULAR,
    "/work/link-out.txt": _LIVE_SYMLINK,
    "/work/sub": _LIVE_DIRECTORY,
    "/work/sub/child.txt": _LIVE_NESTED,
    "/etc": _LIVE_ETC,
    "/etc/hostname": _LIVE_ETC_HOSTNAME,
}
_GUEST_CONTENTS = {
    "/work/real.txt": _REAL_CONTENT,
    "/work/sub/child.txt": b"child",
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
        assert _stat(sandbox, "link-out.txt").kind is EntryKind.OTHER
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

        # `/work` first: a stat walks its parents before describing the entry itself.
        assert client.gets == [
            (f"{_SBX_PATH}/files/stat", {"path": _WORK_DIR, "api-version": _API_VERSION}),
            (
                f"{_SBX_PATH}/files/stat",
                {"path": "/work/real.txt", "api-version": _API_VERSION},
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
        assert _stat(_sandbox(), "real.txt", working_directory="/work/").path == "real.txt"

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
            _stat(_sandbox(), "/work2/real.txt")


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
        return _FakeDataPlaneClient(entries={"/work/real.txt": payload})

    def test_a_payload_without_the_symlink_flag_is_refused(self):
        with pytest.raises(AcasEntryPayloadIncomplete, match="isSymlink"):
            _stat(_sandbox(self._without("isSymlink")), "real.txt")

    def test_a_payload_without_the_directory_flag_is_refused(self):
        with pytest.raises(AcasEntryPayloadIncomplete, match="isDir"):
            _stat(_sandbox(self._without("isDir")), "real.txt")

    def test_a_non_boolean_flag_is_refused(self):
        """A string `"false"` is truthy, so type-checking the flag is not pedantry."""
        payload = {**_LIVE_REGULAR, "isSymlink": "false"}
        client = _FakeDataPlaneClient(entries={"/work/real.txt": payload})
        with pytest.raises(AcasEntryPayloadIncomplete, match="isSymlink"):
            _stat(_sandbox(client), "real.txt")

    def test_the_refusal_is_not_one_a_confinement_check_would_raise(self):
        """`_backend_refusals` translates `ValueError` and `OSError`; this must pass through both."""
        assert not issubclass(AcasEntryPayloadIncomplete, ValueError | OSError)

    def test_an_absent_size_is_unknown_rather_than_zero(self):
        """`None` fails closed upstream; zero would make every cap read that file as free."""
        payload = {k: v for k, v in _LIVE_REGULAR.items() if k != "size"}
        client = _FakeDataPlaneClient(entries={"/work/real.txt": payload})
        assert _stat(_sandbox(client), "real.txt").size_bytes is None

    def test_a_negative_size_is_unknown_too(self):
        """Worse than zero: it clears every cap and is then subtracted from the running total."""
        client = _FakeDataPlaneClient(entries={"/work/real.txt": {**_LIVE_REGULAR, "size": -1}})
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
        assert asyncio.run(client.read_file("/work/link-out.txt")) == _HOSTNAME

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

        client = _FakeDataPlaneClient(contents={"/work/real.txt": b"x" * 4096})
        with pytest.raises(SandboxTransferCapExceeded, match="read back"):
            asyncio.run(
                _sandbox(client).read_file(
                    "real.txt", working_directory=_WORK_DIR, max_bytes=len(_REAL_CONTENT)
                )
            )

    def test_an_unknown_size_is_refused(self):
        from maf_sandbox import SandboxOutputSizeUnknown

        payload = {k: v for k, v in _LIVE_REGULAR.items() if k != "size"}
        client = _FakeDataPlaneClient(entries={"/work/real.txt": payload})
        with pytest.raises(SandboxOutputSizeUnknown):
            asyncio.run(
                _sandbox(client).read_file("real.txt", working_directory=_WORK_DIR, max_bytes=64)
            )

    def test_a_negative_size_is_refused_rather_than_read(self):
        """A negative passes `size_bytes > max_bytes`, so without this the read still happens."""
        from maf_sandbox import SandboxOutputSizeUnknown

        client = _FakeDataPlaneClient(
            entries={**_GUEST_FILESYSTEM, "/work/real.txt": {**_LIVE_REGULAR, "size": -1}}
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
        assert client.reads == ["/work/real.txt"]

    def test_a_nested_path_through_real_directories_still_reads(self):
        """The component walk refuses links, not depth."""
        content = asyncio.run(
            _sandbox().read_file("sub/child.txt", working_directory=_WORK_DIR, max_bytes=64)
        )
        assert content == _GUEST_CONTENTS["/work/sub/child.txt"]

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
        assert client.reads == ["/work/real.txt"]


class TestASymlinkedParentEscapesLexicalConfinement:
    """`ln -sfn /etc /work/out`, the escape a lexical check cannot see.

    Verified against a live sandbox group: `stat out` is OTHER, but `stat out/hostname` is a
    regular 12-byte file, reading it returns `/etc/hostname`, and listing `out` enumerates
    `/etc`. Nothing in the final entry's payload records that a parent was a link, so
    confinement has to stat every component rather than classify the last one.
    """

    @staticmethod
    def _client():
        return _FakeDataPlaneClient(entries={**_GUEST_FILESYSTEM, "/work/out": _LIVE_SYMLINK_DIR})

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
        through = asyncio.run(_sandbox(client)._stat_guest("/work/out/hostname", "out/hostname"))
        assert through.entry.kind is EntryKind.FILE
        assert through.entry.size_bytes == len(_HOSTNAME)
        assert asyncio.run(client.read_file("/work/out/hostname")) == _HOSTNAME

        listed = asyncio.run(
            client._dp_get(
                f"{_SBX_PATH}/files/list",
                params={"path": "/work/out", "api-version": _API_VERSION},
            )
        )
        # Live-verified: the service echoes the REQUESTED prefix, so every escaped entry looks
        # like it sits under the working directory. The per-entry check cannot fire on these —
        # the component walk is the only thing standing between a kind and /etc.
        assert [entry["path"] for entry in listed["entries"]] == ["/work/out/hostname"]

    def test_a_final_component_link_is_described_rather_than_refused(self):
        """Only the parents are refused: reporting a link as `OTHER` is how a caller learns."""
        from maf_sandbox import EntryKind

        assert _stat(_sandbox(self._client()), "out").kind is EntryKind.OTHER

    def test_a_bare_stat_through_a_symlinked_parent_is_refused(self):
        """No bytes escape, but a type and a size do — metadata from outside the boundary."""
        client = self._client()
        with pytest.raises(ValueError, match="real directory"):
            _stat(_sandbox(client), "out/hostname")
        assert client.gets == [self._stat_route(_WORK_DIR), self._stat_route("/work/out")]

    def test_the_escape_is_decided_by_the_payload_flag_not_by_the_entry_kind(self):
        """`OTHER` is the protocol's word for every non-regular entry, not a synonym for link.

        So the walk reads `isSymlink`, and a non-directory that is not one stays `ENOTDIR`.
        """
        from maf_sandbox import EntryKind

        sandbox = _sandbox(self._client())
        link = asyncio.run(sandbox._stat_guest("/work/out", "out"))
        assert link.is_symlink and link.entry.kind is EntryKind.OTHER
        plain = asyncio.run(sandbox._stat_guest("/work/real.txt", "real.txt"))
        assert not plain.is_symlink

    def test_a_read_through_a_symlinked_parent_is_refused(self):
        client = self._client()
        with pytest.raises(ValueError, match="real directory"):
            asyncio.run(
                _sandbox(client).read_file(
                    "out/hostname", working_directory=_WORK_DIR, max_bytes=64
                )
            )
        assert client.reads == []
        assert client.gets == [self._stat_route(_WORK_DIR), self._stat_route("/work/out")]

    def test_a_listing_through_a_symlinked_directory_is_refused(self):
        """The listing is never requested: the walk covers the directory named, not only its parents."""
        client = self._client()
        with pytest.raises(ValueError, match="real directory"):
            asyncio.run(_sandbox(client).list_dir("out", working_directory=_WORK_DIR))
        assert client.gets == [self._stat_route(_WORK_DIR), self._stat_route("/work/out")]

    def test_a_path_through_a_regular_file_is_not_reported_as_an_escape(self):
        """`ENOTDIR` is not a confinement failure, and only a link makes it one."""
        client = self._client()
        with pytest.raises(NotADirectoryError):
            asyncio.run(
                _sandbox(client).read_file(
                    "real.txt/child", working_directory=_WORK_DIR, max_bytes=64
                )
            )


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
            "link-out.txt": EntryKind.OTHER,
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
            entries={_WORK_DIR: _LIVE_WORK_DIR, "/work/real.txt": escaped}
        )
        with pytest.raises(ValueError, match="outside working directory"):
            asyncio.run(_sandbox(client).list_dir(".", working_directory=_WORK_DIR))

    def test_a_listed_entry_without_a_type_flag_is_refused(self):
        payload = {k: v for k, v in _LIVE_REGULAR.items() if k != "isSymlink"}
        client = _FakeDataPlaneClient(
            entries={_WORK_DIR: _LIVE_WORK_DIR, "/work/real.txt": payload}
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
            entries={_WORK_DIR: _LIVE_WORK_DIR, "/work/real.txt": payload}
        )
        with pytest.raises(AcasEntryPayloadIncomplete, match="no 'path'"):
            asyncio.run(_sandbox(client).list_dir(".", working_directory=_WORK_DIR))

    def test_the_directory_itself_is_refused_when_it_is_outside(self):
        with pytest.raises(ValueError, match="outside working directory"):
            asyncio.run(_sandbox().list_dir("/etc", working_directory=_WORK_DIR))

    def test_a_listed_sibling_of_the_directory_is_refused(self):
        """`list_dir("sub")` enumerates one level, so `/work/real.txt` is not an answer to it."""
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
            entries={_WORK_DIR: _LIVE_WORK_DIR, "/work/real.txt": payload}
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
