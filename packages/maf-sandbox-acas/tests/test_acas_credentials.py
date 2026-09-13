"""Authority isolation and ownership, without Azure requests or real credentials."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import HttpResponseError
from azure.core.pipeline import PipelineContext, PipelineRequest
from azure.core.pipeline.policies import AsyncBearerTokenCredentialPolicy
from azure.core.rest import HttpRequest
from maf_sandbox import Capability, SandboxKey
from test_acas_backend import _guest_removing, _GuestGroupClient, _spec_requiring

from maf_sandbox_acas import (
    AcasClientCloseError,
    AcasCredentialBinding,
    AcasCredentialError,
    AcasSandboxBackend,
    AcasSandboxConfig,
)
from maf_sandbox_acas._credentials import ClientPool


class Credential:
    def __init__(self, name="app"):
        self.name = name
        self.loop = asyncio.get_running_loop()
        self.closed = 0

    async def get_token(self, *scopes, **kwargs):
        return AccessToken("synthetic-" + self.name, 4_000_000_000)

    async def close(self):
        assert asyncio.get_running_loop() is self.loop
        self.closed += 1


class Client:
    def __init__(self, credential):
        self.credential = credential
        self.loop = asyncio.get_running_loop()
        self.closed = 0
        self.policy = AsyncBearerTokenCredentialPolicy(
            credential, "https://example.invalid/.default"
        )

    async def header(self):
        assert not self.closed
        request = PipelineRequest(
            HttpRequest("GET", "https://example.invalid/"), PipelineContext(None)
        )
        await self.policy.on_request(request)
        return request.http_request.headers["Authorization"]

    async def close(self):
        assert asyncio.get_running_loop() is self.loop
        self.closed += 1


def binding(name="app", generation="1"):
    return AcasCredentialBinding(name, generation, lambda: Credential(name + generation))


def pool(*, capacity=2, wait=1, close=1, build=Client):
    return ClientPool(build, capacity=capacity, wait_seconds=wait, close_seconds=close)


def test_pipeline_token_cache_is_partitioned_by_grant_and_generation():
    async def scenario():
        clients = pool(capacity=3)
        headers = []
        for grant in (binding("a"), binding("b"), binding("a", "2")):
            async with clients.lease(grant) as client:
                headers.append(await client.header())
        assert len(set(headers)) == 3
        await clients.aclose()

    asyncio.run(scenario())


def test_idle_eviction_closes_only_the_idle_entry_and_nested_leases_fit_capacity_one():
    async def scenario():
        clients = pool(capacity=1)
        async with clients.lease(binding("a")) as first:
            async with clients.lease(binding("a")) as nested:
                assert nested is first
            assert not first.closed
        async with clients.lease(binding("b")) as second:
            assert first.closed == first.credential.closed == 1
            assert not second.closed
        async with clients.lease(binding("a")) as rebuilt:
            assert rebuilt is not first
        await clients.aclose()
        assert second.closed == second.credential.closed == 1

    asyncio.run(scenario())


def test_full_cache_wait_is_bounded_without_closing_a_busy_client():
    async def scenario():
        clients = pool(capacity=1, wait=0.02)
        async with clients.lease(binding("a")) as first:
            with pytest.raises(AcasCredentialError):
                async with clients.lease(binding("b")):
                    pytest.fail("over capacity")
            assert not first.closed
        await clients.aclose()

    asyncio.run(scenario())


def test_single_flight_construction_survives_one_waiter_cancelling():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        made = []

        async def factory():
            made.append(Credential())
            started.set()
            await release.wait()
            return made[-1]

        grant = AcasCredentialBinding("app", "1", factory)
        clients = pool()

        async def use():
            async with clients.lease(grant) as client:
                return client

        first = asyncio.create_task(use())
        await started.wait()
        second = asyncio.create_task(use())
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        result = await second
        assert result.credential is made[0] and len(made) == 1
        await clients.aclose()
        assert made[0].closed == 1

    asyncio.run(scenario())


def test_cancelled_sole_constructor_releases_capacity():
    async def scenario():
        started, finished = asyncio.Event(), asyncio.Event()

        async def factory():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        clients = pool(capacity=1)

        async def use():
            async with clients.lease(AcasCredentialBinding("slow", "1", factory)):
                pytest.fail("cancelled construction completed")

        pending = asyncio.create_task(use())
        await started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await finished.wait()
        async with clients.lease(binding("next")):
            pass
        await clients.aclose()

    asyncio.run(scenario())


def test_failed_client_constructor_closes_the_credential_and_redacts_its_error():
    async def scenario():
        created = []

        def fail(credential):
            created.append(credential)
            raise ValueError("synthetic-private-assertion")

        clients = pool(build=fail)
        with pytest.raises(AcasCredentialError) as error:
            async with clients.lease(binding()):
                pass
        assert "synthetic-private" not in str(error.value)
        assert created[0].closed == 1
        await clients.aclose()

    asyncio.run(scenario())


def test_shutdown_drains_an_operation_and_refuses_new_leases():
    async def scenario():
        clients = pool()
        async with clients.lease(binding()) as client:
            closing = asyncio.create_task(clients.aclose())
            await asyncio.sleep(0)
            assert not client.closed
            with pytest.raises(AcasCredentialError):
                async with clients.lease(binding("new")):
                    pass
        await closing
        assert client.closed == client.credential.closed == 1
        await clients.aclose()
        assert client.closed == 1

    asyncio.run(scenario())


def test_shutdown_timeout_keeps_busy_resources_for_retry():
    async def scenario():
        clients = pool(close=0.02)
        async with clients.lease(binding()) as client:
            with pytest.raises(AcasClientCloseError):
                await clients.aclose()
            assert not client.closed
        await clients.aclose()
        assert client.closed == client.credential.closed == 1

    asyncio.run(scenario())


def test_shutdown_dispatches_to_live_owner_loops():
    clients = pool()
    started = threading.Event()
    owner = asyncio.new_event_loop()

    def run_loop():
        asyncio.set_event_loop(owner)
        owner.call_soon(started.set)
        owner.run_forever()
        owner.close()

    async def use():
        async with clients.lease(binding()) as client:
            return client

    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(run_loop)
        assert started.wait(5)
        try:
            first = asyncio.run_coroutine_threadsafe(use(), owner).result(5)

            async def close():
                second = await use()
                assert first is not second
                await clients.aclose()
                assert first.closed == second.closed == 1

            asyncio.run(close())
        finally:
            owner.call_soon_threadsafe(owner.stop)
            running.result(5)


def test_stopped_owner_loop_reports_incomplete_cleanup_and_can_be_resumed():
    clients = pool()
    owner = asyncio.new_event_loop()

    async def use():
        async with clients.lease(binding()) as client:
            return client

    try:
        client = owner.run_until_complete(use())
        with pytest.raises(AcasClientCloseError):
            asyncio.run(clients.aclose())
        assert not client.closed
        owner.run_until_complete(clients.aclose())
        assert client.closed == client.credential.closed == 1
    finally:
        owner.close()


class Service(_GuestGroupClient):
    def __init__(self):
        super().__init__(_guest_removing(True))
        self.labels = {}

    async def begin_create_sandbox(self, *, labels, **kwargs):
        poller = await super().begin_create_sandbox(labels=labels, **kwargs)
        sandbox = await poller.result()
        self.labels[sandbox.sandbox_id] = labels
        return poller

    async def list_sandboxes(self, *, labels=None):
        for identity, actual in self.labels.items():
            if identity not in self.deleted and all(
                actual.get(k) == v for k, v in (labels or {}).items()
            ):
                yield SimpleNamespace(id=identity)


class Group(Client):
    def __init__(self, credential, service, calls):
        super().__init__(credential)
        self.service, self.calls = service, calls

    def get_sandbox_client(self, identity):
        group = self
        underlying = self.service.get_sandbox_client(identity)

        class SandboxClient:
            sandbox_id = identity

            def __getattr__(self, name):
                target = getattr(underlying, name)
                if not callable(target):
                    return target

                async def call(*args, **kwargs):
                    group.calls.append((group.credential.name, name))
                    assert not group.closed
                    assert asyncio.get_running_loop() is group.loop
                    result = target(*args, **kwargs)
                    assert inspect.isawaitable(result)
                    return await result

                return call

        return SandboxClient()

    async def begin_create_sandbox(self, **kwargs):
        self.calls.append((self.credential.name, "create"))
        return await self.service.begin_create_sandbox(**kwargs)

    async def list_sandboxes(self, **kwargs):
        self.calls.append((self.credential.name, "list"))
        async for item in self.service.list_sandboxes(**kwargs):
            assert not self.closed
            yield item


def backend(service, resolver, *, capacity=2):
    subject = AcasSandboxBackend(
        AcasSandboxConfig(
            endpoint="https://example.invalid",
            credential_resolver=resolver,
            max_clients_per_loop=capacity,
        )
    )
    calls, built = [], []

    def build(credential):
        result = Group(credential, service, calls)
        built.append(result)
        return result

    subject._group_client = build
    return subject, calls, built


def test_same_scope_wrappers_capture_distinct_authority_and_survive_eviction():
    async def scenario():
        caller = contextvars.ContextVar("caller", default="a")

        async def resolve(request):
            return binding(caller.get())

        subject, calls, built = backend(Service(), resolve, capacity=1)
        key = SandboxKey("shared-scope", "thread", "agent")
        spec = _spec_requiring(Capability.EXEC)
        first = await subject.acquire(key, spec)
        caller.set("b")
        second = await subject.acquire(key, spec)
        assert first.instance_id == second.instance_id
        assert built[0].closed == 1
        calls.clear()
        await first._exec_text("true", working_directory="/", timeout=1)
        await second._exec_text("true", working_directory="/", timeout=1)
        assert calls == [("a1", "exec"), ("b1", "exec")]
        await subject.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("scope_wide", [False, True])
def test_another_replica_cleans_label_discovered_resources_without_request_context(scope_wide):
    async def scenario():
        context = contextvars.ContextVar("request")
        context.set("caller")
        requests = []

        async def resolve(request):
            requests.append(request)
            return binding(
                context.get() if request.operation == "acquire" else "cleanup:" + request.scope
            )

        service = Service()
        first, _, _ = backend(service, resolve)
        key = SandboxKey("scope", "thread", "agent")
        created = await first.acquire(key, _spec_requiring(Capability.EXEC))
        identity = created.instance_id
        await first.aclose()
        del created, first
        context.set(None)
        second, calls, _ = backend(service, resolve)
        assert not second._registry and not second._undeleted
        if scope_wide:
            result = await second.dispose_scope(key.scope, key.thread_id)
            assert result.disposed == 1 and result.undisposed is None
        else:
            assert await second.dispose(key) is None
        assert identity in service.deleted
        assert all(principal == "cleanup:scope1" for principal, _ in calls)
        assert requests[-1].operation == ("dispose_scope" if scope_wide else "dispose")
        await second.aclose()

    asyncio.run(scenario())


def test_denied_cleanup_is_reported_and_a_new_replica_can_retry():
    async def scenario():
        deny = False

        async def resolve(request):
            if deny:
                raise ValueError("synthetic-secret")
            return binding("cleanup" if request.operation != "acquire" else "caller")

        service = Service()
        first, _, _ = backend(service, resolve)
        key = SandboxKey("scope", "thread", "agent")
        created = await first.acquire(key, _spec_requiring(Capability.EXEC))
        deny = True
        failure = await first.dispose(key)
        assert failure is not None and "synthetic-secret" not in failure.detail
        assert first._undeleted and not service.deleted
        await first.aclose()
        deny = False
        second, _, _ = backend(service, resolve)
        assert await second.dispose(key) is None
        assert created.instance_id in service.deleted
        assert await second.dispose(key) is None
        await second.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 403])
def test_warm_permission_failure_never_creates_a_replacement(status, monkeypatch):
    async def scenario():
        async def resolve(request):
            return binding()

        service = Service()
        subject, _, _ = backend(service, resolve)
        key = SandboxKey("scope", "thread", "agent")
        spec = _spec_requiring(Capability.EXEC)
        original = await subject.acquire(key, spec)

        async def denied(self, **kwargs):
            error = HttpResponseError("denied")
            error.status_code = status
            raise error

        from test_acas_backend import _GuestSandboxClient

        monkeypatch.setattr(_GuestSandboxClient, "ensure_running", denied)
        with pytest.raises(HttpResponseError):
            await subject.acquire(key, spec)
        assert service.create_calls == 1
        assert next(iter(subject._registry.values())).sandbox_id == original.instance_id
        await subject.aclose()

    asyncio.run(scenario())


def test_close_failure_does_not_skip_other_resources_and_retries_only_unclosed_resources():
    async def scenario():
        failing = True

        class Unreliable(Client):
            async def close(self):
                if failing and self.credential.name == "a1":
                    raise RuntimeError("close failed")
                await super().close()

        clients = pool(build=Unreliable)
        async with clients.lease(binding("a")) as first:
            pass
        async with clients.lease(binding("b")) as second:
            pass
        with pytest.raises(AcasClientCloseError):
            await clients.aclose()
        assert not first.closed
        assert first.credential.closed == second.closed == second.credential.closed == 1
        failing = False
        await clients.aclose()
        assert first.closed == first.credential.closed == second.closed == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["acquire", "dispose", "dispose_scope"])
def test_resolver_timeout_is_bounded_and_never_constructs_default_credentials(operation):
    async def scenario():
        async def resolve(request):
            await asyncio.Event().wait()

        subject = AcasSandboxBackend(
            AcasSandboxConfig(
                endpoint="https://example.invalid",
                credential_resolver=resolve,
                client_wait_seconds=0.02,
            )
        )
        subject._group_client = lambda credential: pytest.fail("credential fallback")
        key = SandboxKey("scope", "thread", "agent")
        if operation == "acquire":
            with pytest.raises(AcasCredentialError):
                await subject.acquire(key, _spec_requiring(Capability.EXEC))
        elif operation == "dispose":
            assert await subject.dispose(key) is not None
        else:
            assert (await subject.dispose_scope(key.scope, key.thread_id)).undisposed is not None
        await subject.aclose()

    asyncio.run(scenario())


def test_retained_deletion_during_acquire_resolves_cleanup_before_request_at_capacity_one():
    async def scenario():
        operations = []

        async def resolve(request):
            operations.append(request.operation)
            return binding("caller" if request.operation == "acquire" else "cleanup")

        service = Service()
        subject, calls, _ = backend(service, resolve, capacity=1)
        key = SandboxKey("scope", "thread", "agent")
        spec = _spec_requiring(Capability.EXEC)
        await subject.acquire(key, spec)
        service.delete_fails = True
        assert await subject.dispose(key) is not None
        operations.clear()
        calls.clear()
        service.delete_fails = False
        await subject.acquire(key, spec)
        assert operations == ["dispose", "acquire"]
        assert ("cleanup1", "begin_delete") in calls
        assert ("caller1", "create") in calls
        await subject.aclose()

    asyncio.run(scenario())


def test_stream_keeps_its_client_until_response_closure_even_when_cancelled():
    from maf_sandbox_acas._backend import _AcasSandbox

    async def scenario():
        reading, release, response_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Response:
            headers = {}
            status_code = 200

            async def iter_raw(self):
                reading.set()
                await release.wait()
                yield b'{"stdout":"ok","stderr":"","exitCode":0}'

            async def close(self):
                response_closed.set()

        class Streaming(Client):
            async def send(self, request, **kwargs):
                return SimpleNamespace(http_response=Response())

            def get_sandbox_client(self, identity):
                return SimpleNamespace(
                    sandbox_id=identity,
                    _endpoint="https://example.invalid",
                    _sbx_path="/sandboxes/one",
                    _api_version="test",
                    _pipeline=SimpleNamespace(run=self.send),
                )

        clients = pool(capacity=1, build=Streaming)
        async with clients.lease(binding()) as group:
            sandbox = _AcasSandbox(
                group.get_sandbox_client("one"), 1, pool=clients, binding=binding()
            )
        running = asyncio.create_task(
            sandbox._exec_text_bounded(
                "true",
                working_directory="/",
                timeout=1,
                max_output_bytes=256,
            )
        )
        await reading.wait()
        closing = asyncio.create_task(clients.aclose())
        await asyncio.sleep(0)
        assert not group.closed
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert response_closed.is_set()
        await closing
        assert group.closed == group.credential.closed == 1

    asyncio.run(scenario())


def test_background_invalidation_uses_the_wrappers_captured_grant():
    async def scenario():
        caller = contextvars.ContextVar("caller", default="a")

        async def resolve(request):
            return binding(caller.get())

        subject, calls, _ = backend(Service(), resolve, capacity=1)
        sandbox = await subject.acquire(
            SandboxKey("scope", "thread", "agent"), _spec_requiring(Capability.EXEC)
        )
        caller.set("b")
        calls.clear()
        await sandbox._invalidate_after_exec(RuntimeError("capture failed"))
        assert calls == [("a1", "begin_delete")]
        await subject.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_clients_per_loop", 0),
        ("max_clients_per_loop", True),
        ("client_wait_seconds", 0),
        ("client_wait_seconds", float("nan")),
        ("client_close_seconds", float("inf")),
        ("client_close_seconds", -1),
        ("credential_resolver", "invalid"),
    ],
)
def test_invalid_credential_configuration_is_rejected(field, value):
    with pytest.raises(ValueError, match=field):
        AcasSandboxConfig(endpoint="https://example.invalid", **{field: value})


@pytest.mark.parametrize("status", [401, 403])
def test_denied_lifecycle_configuration_refuses_and_cleans_up(status, monkeypatch):
    from test_acas_backend import _GuestSandboxClient

    async def scenario():
        async def resolve(request):
            return binding()

        async def denied(self, policy):
            error = HttpResponseError("denied")
            error.status_code = status
            raise error

        monkeypatch.setattr(_GuestSandboxClient, "set_lifecycle_policy", denied)
        service = Service()
        subject, _, _ = backend(service, resolve)
        with pytest.raises(HttpResponseError):
            await subject.acquire(
                SandboxKey("scope", "thread", "agent"), _spec_requiring(Capability.EXEC)
            )
        assert service.deleted == ["sbx-1"] and not subject._registry
        await subject.aclose()

    asyncio.run(scenario())


def test_acquire_admitted_before_shutdown_finishes_its_nested_operations(monkeypatch):
    from test_acas_backend import _GuestSandboxClient

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def configure(self, policy):
            entered.set()
            await release.wait()

        async def resolve(request):
            return binding()

        monkeypatch.setattr(_GuestSandboxClient, "set_lifecycle_policy", configure)
        subject, _, built = backend(Service(), resolve, capacity=1)
        acquiring = asyncio.create_task(
            subject.acquire(
                SandboxKey("scope", "thread", "agent"),
                _spec_requiring(Capability.EXEC),
            )
        )
        await entered.wait()
        closing = asyncio.create_task(subject.aclose())
        await asyncio.sleep(0)
        assert not built[0].closed
        release.set()
        sandbox = await acquiring
        assert sandbox.instance_id == "sbx-1"
        await closing
        assert built[0].closed == 1

    asyncio.run(scenario())


def test_inherited_context_never_reuses_a_client_on_another_loop():
    owner = asyncio.new_event_loop()
    started = threading.Event()

    def run_loop():
        asyncio.set_event_loop(owner)
        owner.call_soon(started.set)
        owner.run_forever()
        owner.close()

    async def scenario():
        async def resolve(request):
            return binding()

        subject, _, built = backend(Service(), resolve)
        sandbox = await subject.acquire(
            SandboxKey("scope", "thread", "agent"), _spec_requiring(Capability.EXEC)
        )
        async with sandbox.client_lease():
            remote = asyncio.run_coroutine_threadsafe(
                sandbox._exec_text("true", working_directory="/", timeout=1),
                owner,
            )
            await asyncio.wrap_future(remote)
        assert len(built) == 2 and built[0].loop is not built[1].loop
        await subject.aclose()
        assert all(client.closed == 1 for client in built)

    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(run_loop)
        assert started.wait(5)
        try:
            asyncio.run(scenario())
        finally:
            owner.call_soon_threadsafe(owner.stop)
            running.result(5)


def test_a_loop_with_no_owned_resources_does_not_need_to_survive_shutdown():
    def fail(credential):
        raise ValueError("cannot construct client")

    clients = pool(build=fail)

    async def use():
        with pytest.raises(AcasCredentialError):
            async with clients.lease(binding()):
                pass

    asyncio.run(use())
    asyncio.run(clients.aclose())
