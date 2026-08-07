"""Offline tests for the ACA Sandboxes backend.

No live sandbox group and no host application: the group client is replaced by a fake, and
the disk-image tests build the **real** SDK dataclasses so the shape they assert is the
SDK's rather than one the code and the fake happen to agree on.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from maf_sandbox import Isolation, SandboxBackend, SandboxKey

from maf_sandbox_aca import (
    AcaSandboxBackend,
    AcaSandboxConfig,
    disk_image_base,
    resolve_disk_image_id,
)

_ENDPOINT = "https://management.example.azuredevcompute.io"


def _config(**overrides) -> AcaSandboxConfig:
    return AcaSandboxConfig(endpoint=_ENDPOINT, **overrides)


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


def _backend_with(group_client, config: AcaSandboxConfig | None = None) -> AcaSandboxBackend:
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
        from maf_sandbox_aca._images import qualify_image_reference

        assert qualify_image_reference("acr.azurecr.io", "bicep-sandbox:0.46.1") == (
            "acr.azurecr.io/bicep-sandbox:0.46.1"
        )

    def test_a_tag_colon_is_not_mistaken_for_a_port(self):
        """`bicep-sandbox:0.46.1` has a colon but no registry — the trap in this rule."""
        from maf_sandbox_aca._images import qualify_image_reference

        # Whole-string equality rather than a prefix check: `startswith` on something that
        # looks like a URL is the shape of an incomplete-sanitization bug, and a scanner
        # cannot tell an assertion from a security check. The full form is stricter anyway.
        assert qualify_image_reference("acr.azurecr.io", "img:1.2.3") == "acr.azurecr.io/img:1.2.3"

    def test_leaves_an_already_qualified_reference_alone(self):
        """Double-prefixing surfaces only as "no disk image was built from …", far away."""
        from maf_sandbox_aca._images import qualify_image_reference

        assert qualify_image_reference("acr.azurecr.io", "other.azurecr.io/img:1") == (
            "other.azurecr.io/img:1"
        )

    def test_a_repository_path_is_not_a_registry(self):
        from maf_sandbox_aca._images import qualify_image_reference

        assert qualify_image_reference("acr.azurecr.io", "library/ubuntu:22.04") == (
            "acr.azurecr.io/library/ubuntu:22.04"
        )

    def test_localhost_and_ports_count_as_registries(self):
        from maf_sandbox_aca._images import qualify_image_reference

        assert qualify_image_reference("acr.io", "localhost/img:1") == "localhost/img:1"
        assert qualify_image_reference("acr.io", "reg:5000/img:1") == "reg:5000/img:1"

    def test_no_registry_configured_leaves_the_image_untouched(self):
        from maf_sandbox_aca._images import qualify_image_reference

        assert qualify_image_reference("", "img:1") == "img:1"

    def test_a_trailing_slash_on_the_registry_does_not_double_up(self):
        from maf_sandbox_aca._images import qualify_image_reference

        assert qualify_image_reference("acr.azurecr.io/", "img:1") == "acr.azurecr.io/img:1"


class TestResolveDiskImageId:
    def setup_method(self):
        from maf_sandbox_aca._images import _disk_image_cache

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
        from maf_sandbox_aca._backend import _AcaSandbox

        class _RecordingClient:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            async def write_file(self, path, content, **kwargs):
                self.calls.append((path, content, kwargs))

        client = _RecordingClient()
        asyncio.run(_AcaSandbox(client).write_file("/work/infra/main.bicep", "param x string"))

        assert client.calls == [("/work/infra/main.bicep", "param x string", {"create_dirs": True})]


class TestExecArgv:
    """`_AcaSandbox.exec` accepts a sequence and quotes it before the SDK's string-only exec.

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
        from maf_sandbox_aca._backend import _AcaSandbox

        client = self._RecordingClient()
        asyncio.run(_AcaSandbox(client).exec("echo hi", working_directory="/work", timeout=5))
        assert client.calls == ["echo hi"]

    def test_a_sequence_is_quoted_with_shlex_join(self):
        import shlex

        from maf_sandbox_aca._backend import _AcaSandbox

        client = self._RecordingClient()
        argv = ["echo", "a; rm -rf /", "$(id)", "`id`", "it's mine", 'say "hi"']
        asyncio.run(_AcaSandbox(client).exec(argv, working_directory="/work", timeout=5))

        assert client.calls == [shlex.join(argv)]
        # Round-tripping through shlex.split recovers the exact argv — proof the quoted
        # form cannot be re-interpreted as more than one token per element, and cannot
        # break out into a second shell command.
        assert shlex.split(client.calls[0]) == argv

    def test_a_bare_space_separated_argv_stays_one_command(self):
        import shlex

        from maf_sandbox_aca._backend import _AcaSandbox

        client = self._RecordingClient()
        argv = ["bicep", "build", "/acas/work/r1/main.bicep", "--diagnostics-format", "sarif"]
        asyncio.run(_AcaSandbox(client).exec(argv, working_directory="/work", timeout=5))

        assert shlex.split(client.calls[0]) == argv


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
# Lifecycle visibility — a VM started or reclaimed must leave a record
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
        from maf_sandbox_aca._backend import _label_value

        assert _label_value("scope-a") == "scope-a"
        assert _label_value("x" * 63) == "x" * 63

    def test_long_values_are_digested_within_the_limit(self):
        from maf_sandbox_aca._backend import _LABEL_VALUE_MAX, _label_value

        out = _label_value("y" * 200)
        assert len(out) <= _LABEL_VALUE_MAX
        assert out.startswith("sha256-")

    def test_values_sharing_a_long_prefix_do_not_collide(self):
        """Truncation would map these together; these labels gate one user's purge."""
        from maf_sandbox_aca._backend import _label_value

        a = "user-" + "z" * 90 + "AAAA"
        b = "user-" + "z" * 90 + "BBBB"
        assert _label_value(a) != _label_value(b)

    def test_create_and_purge_agree_on_the_label(self):
        """The round trip: what acquire writes must be what dispose_scope queries.

        Applying the mapping on one side only would not raise — the listing would simply
        match nothing, and every sandbox for a deleted conversation would keep running.
        """
        from maf_sandbox import SandboxSpec

        from maf_sandbox_aca._backend import _LABEL_SCOPE, _LABEL_THREAD, _sandbox_labels

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

        from maf_sandbox_aca._backend import _LABEL_VALUE_MAX, _sandbox_labels

        key = SandboxKey(scope=self._LONG_SCOPE, thread_id="t" * 120, agent_dir="a" * 90)
        labels = _sandbox_labels(key, SandboxSpec(kind="bicep", labels={"extra": "e" * 200}))

        oversized = {k: len(v) for k, v in labels.items() if len(v) > _LABEL_VALUE_MAX}
        assert oversized == {}, oversized


class TestLifecycleLogging:
    """Acquire and release must say what happened, at INFO.

    None of it is inferable from the tool's output: `bicep_validate` returns the same
    compiler diagnostics whether a warm sandbox was reused in a second or a cold VM was
    created in a minute, and a sandbox that is never released is billable but silent.
    The operator-facing question — was one created, was it used, was it released — has no
    other answer, so these lines are load-bearing rather than decoration.
    """

    def test_reuse_is_logged(self, caplog):
        client = _FakeGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._registry[(key.scope, key.thread_id, key.agent_dir)] = "sbx-warm"

        from maf_sandbox import SandboxSpec

        with caplog.at_level(logging.INFO, logger="maf_sandbox_aca"):
            asyncio.run(backend.acquire(key, SandboxSpec(kind="bicep", image="img:1")))

        assert any("sandbox reused" in r.getMessage() for r in caplog.records), caplog.text
        assert any("sbx-warm" in r.getMessage() for r in caplog.records)

    def test_release_is_logged(self, caplog):
        client = _FakeGroupClient()
        backend = _backend_with(client)
        key = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
        backend._registry[(key.scope, key.thread_id, key.agent_dir)] = "sbx-1"

        with caplog.at_level(logging.INFO, logger="maf_sandbox_aca"):
            asyncio.run(backend.dispose(key))

        assert any("sandbox released" in r.getMessage() for r in caplog.records), caplog.text

    def test_a_scope_purge_names_each_sandbox_it_deletes(self, caplog):
        client = _FakeGroupClient(sandboxes=[_FakeSandbox("sbx-a"), _FakeSandbox("sbx-b")])
        backend = _backend_with(client)

        with caplog.at_level(logging.INFO, logger="maf_sandbox_aca"):
            count = asyncio.run(backend.dispose_scope("scope-a", "thread-1"))

        assert count == 2
        released = [r for r in caplog.records if "sandbox released" in r.getMessage()]
        assert len(released) == 2, caplog.text


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
        backend._registry[(key.scope, key.thread_id, key.agent_dir)] = "sbx-warm"

        from maf_sandbox import SandboxSpec

        with caplog.at_level(logging.INFO, logger="maf_sandbox_aca"):
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
        backend._registry[(key.scope, key.thread_id, key.agent_dir)] = "sbx-1"

        with caplog.at_level(logging.WARNING, logger="maf_sandbox_aca"):
            asyncio.run(backend.dispose(key))

        assert "status=400" in caplog.text, caplog.text
        assert "principal lacks a role" in caplog.text, caplog.text
        failed = [r for r in caplog.records if "failed to delete sandbox" in r.getMessage()]
        assert len(failed) == 1
        assert failed[0].msg == "aca backend: failed to delete sandbox %s: %s"

    def test_list_failure_logs_status_and_body(self, caplog):
        class _ListFailsGroupClient:
            def list_sandboxes(self, *, labels=None):
                raise _HttpError()

        backend = _backend_with(_ListFailsGroupClient())

        with caplog.at_level(logging.WARNING, logger="maf_sandbox_aca"):
            asyncio.run(backend.dispose_scope("scope-a", "thread-1"))

        assert "status=400" in caplog.text, caplog.text
        assert "principal lacks a role" in caplog.text, caplog.text
        failed = [
            r for r in caplog.records if "could not list sandboxes for thread" in r.getMessage()
        ]
        assert len(failed) == 1
        assert failed[0].msg == "aca backend: could not list sandboxes for thread %s: %s"

    def test_the_model_facing_surface_is_unaffected(self):
        """This is a log-content-only change: `error_detail` never reaches a tool result.

        Nothing in this backend returns `error_detail`'s output to a caller — it is only
        ever handed to `logger.warning`/`logger.info`. This guards that boundary staying
        true rather than re-deriving it by reading the source on every review.
        """
        import inspect

        from maf_sandbox_aca import _backend

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

        backend = AcaSandboxBackend(_config())
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

        backend = AcaSandboxBackend(_config())
        policy = backend._egress_policy(SandboxSpec(kind="t"))

        assert policy.default_action == "Deny"
        assert policy.host_rules == []


# ---------------------------------------------------------------------------
# Independence from the host application — the invariant the split exists for
# ---------------------------------------------------------------------------

#: The one place this distribution names the application it currently ships inside.  It is
#: here because the guard below needs something to look for; everywhere else the host is
#: referred to by role, so moving this tree to its own repository is a file move plus this
#: single line.
_HOST_PACKAGE = "ats"


class TestNoHostDependency:
    """This package must not import the application it currently ships inside.

    Everything else here would keep passing if someone added ``from <host>.config import
    Settings`` to a module — the tests run in a process where the host package is
    importable, so the coupling would be invisible until the day someone tried to extract
    the package.  A source scan suffices: this backend's own imports are stdlib,
    ``maf_sandbox`` and ``azure-*`` (see :mod:`maf_sandbox_aca._backend`), so the host
    cannot arrive transitively.  Each sandbox distribution carries its own copy of this scan
    over its own sources, so extracting any one of them keeps its guard.
    """

    def _sources(self):
        import pathlib

        import maf_sandbox_aca

        root = pathlib.Path(maf_sandbox_aca.__file__).parent  # type: ignore[arg-type]
        distribution = root.parent.parent
        paths = []
        for directory in (root, distribution / "tests", distribution / "scripts"):
            if directory.is_dir():
                paths.extend(directory.rglob("*.py"))
        return paths

    def test_sources_exist(self):
        """Guards the scan below against silently finding nothing."""
        assert len(self._sources()) >= 7

    def test_nothing_imports_the_host_application(self):
        import re

        host = re.escape(_HOST_PACKAGE)
        pattern = re.compile(rf"(?m)^\s*(?:from\s+{host}[.\s]|import\s+{host}[.\s])")
        offenders = [
            str(p) for p in self._sources() if pattern.search(p.read_text(encoding="utf-8"))
        ]
        assert offenders == [], (
            f"these files import the host application ({_HOST_PACKAGE!r}): {offenders}. "
            "The dependency belongs in the host's own adapter module, reaching this "
            "package through WorkspaceContext and the router."
        )
