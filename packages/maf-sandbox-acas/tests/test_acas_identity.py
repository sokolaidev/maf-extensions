"""Identity admission, credential ownership and cleanup without live Azure access."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from azure.containerapps.sandbox import SandboxGroup
from azure.core.exceptions import HttpResponseError
from maf_sandbox import (
    Capability,
    IdentityScope,
    SandboxCapabilityNotSupported,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
)
from test_acas_backend import _config, _spec_requiring
from test_acas_client_pool import _Credential
from test_acas_credentials import Client, Group, Service, binding

from maf_sandbox_acas import (
    AcasClientCloseError,
    AcasIdentityVerificationError,
    AcasSandboxBackend,
)
from maf_sandbox_acas._identity import GroupClients, _check_group

KEY = SandboxKey("scope", "thread", "agent")
SPEC = _spec_requiring(Capability.EXEC)
GROUP = SandboxGroup(
    id="/subscriptions/subscription/resourceGroups/resource-group"
    "/providers/Microsoft.App/sandboxGroups/group",
    name="group",
    properties={"provisioningState": "Succeeded"},
)
assert GROUP.id is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"id": None},
        {"id": GROUP.id + "-other"},
        {"name": "other"},
        {"properties": {}},
        {"properties": []},
        {"properties": {"provisioningState": "Updating"}},
        {"identity": []},
        {"identity": False},
        {"identity": {}},
        {"identity": {"type": "SystemAssigned"}},
        {"identity": {"type": "UserAssigned", "userAssignedIdentities": {}}},
        {"identity": {"type": "SystemAssigned,UserAssigned"}},
        {"identity": {"type": "future-identity"}},
        {"identity": {"type": "None", "userAssignedIdentities": {"principal": {}}}},
        {"identity": {"type": "None", "userAssignedIdentities": None}},
        {"identity": {"type": "None", "principalId": "principal"}},
        {"identity": {"type": "None", "tenantId": "tenant"}},
        {"identity": {"type": "None", "futureChannel": {}}},
    ],
)
def test_unverifiable_or_attached_group_is_refused(changes):
    with pytest.raises(AcasIdentityVerificationError):
        _check_group(replace(GROUP, **changes), _config())


@pytest.mark.parametrize(
    "identity", [None, {"type": "None"}, {"type": "None", "userAssignedIdentities": {}}]
)
def test_absent_identity_and_arm_resource_casing(identity):
    assert GROUP.id is not None
    _check_group(replace(GROUP, id=GROUP.id.upper(), name="GROUP", identity=identity), _config())


class Inspection:
    def __init__(self):
        self.group = GROUP
        self.error: HttpResponseError | None = None
        self.reads = []
        self.clients = []
        self.wait: Callable[[], Awaitable[None]] | None = None

    def client(self, credential):
        inspection = self

        class Management(Client):
            async def get_group(self, name):
                assert asyncio.get_running_loop() is self.loop and not self.closed
                inspection.reads.append((self.credential.name, name, self.loop))
                if inspection.wait is not None:
                    await inspection.wait()
                if inspection.error is not None:
                    raise inspection.error
                return inspection.group

        client = Management(credential)
        self.clients.append(client)
        return client


def backend(inspection, service=None, *, resolver=None, timeout: float = 1):
    subject = AcasSandboxBackend(
        _config(
            credential_resolver=resolver,
            identity_check_seconds=timeout,
            max_clients_per_loop=1,
        )
    )
    data = []
    service = service or Service()

    def build(credential):
        client = Group(credential, service, [])
        data.append(client)
        return GroupClients(client, lambda: inspection.client(credential))

    subject._group_client = build
    return subject, service, data


async def resolve(request):
    return binding(request.operation)


def test_every_replica_checks_cold_and_warm_acquire_under_selected_authority():
    async def scenario():
        inspection = Inspection()
        caller = contextvars.ContextVar("caller", default="first")

        async def select(request):
            return binding(caller.get())

        first, service, data = backend(inspection, resolver=select)
        second, _, other_data = backend(inspection, service, resolver=select)
        for subject in (first, second):
            cold = await subject.acquire(KEY, SPEC)
            warm = await subject.acquire(KEY, SPEC)
            assert cold.instance_id == warm.instance_id
            caller.set("second")
        assert [name for name, _, _ in inspection.reads] == ["first1"] * 2 + ["second1"] * 2
        assert len(inspection.clients) == 2
        assert all(
            m.credential is d.credential for m, d in zip(inspection.clients, data + other_data)
        )
        await first.aclose()
        await second.aclose()
        assert all(m.closed == m.credential.closed == 1 for m in inspection.clients)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["attachment", 401, 403, 404, 429, 503])
def test_warm_drift_or_read_failure_never_serves_from_cached_success(failure):
    async def scenario():
        inspection = Inspection()
        subject, service, data = backend(inspection, resolver=resolve)
        sandbox = await subject.acquire(KEY, SPEC)
        before = list(data[0].calls)
        if failure == "attachment":
            inspection.group = replace(GROUP, identity={"type": "SystemAssigned"})
        else:
            inspection.error = HttpResponseError("sensitive provider diagnostic")
            inspection.error.status_code = failure
        with pytest.raises(AcasIdentityVerificationError) as caught:
            await subject.acquire(KEY, SPEC)
        assert "sensitive" not in str(caught.value)
        assert caught.value.status_code == (None if failure == "attachment" else failure)
        assert len(inspection.reads) == 2
        assert data[0].calls == before
        assert len(subject._registry) == 1 and not service.deleted
        assert await subject.dispose(KEY) is None
        assert service.deleted == [sandbox.instance_id]
        assert len(inspection.reads) == 2
        await subject.aclose()

    asyncio.run(scenario())


def test_cold_attached_group_is_refused_without_changing_or_deleting_service_resources():
    async def scenario():
        inspection = Inspection()
        inspection.group = replace(GROUP, identity={"type": "UserAssigned"})
        subject, service, data = backend(inspection, resolver=resolve)
        with pytest.raises(AcasIdentityVerificationError):
            await subject.acquire(KEY, SPEC)
        assert not data[0].calls and not service.labels and not service.deleted
        await subject.aclose()

    asyncio.run(scenario())


def test_management_construction_failure_leaves_data_and_credentials_owned_for_cleanup():
    async def scenario():
        inspection = Inspection()

        def fail(credential):
            raise RuntimeError("sensitive construction diagnostic")

        inspection.client = fail
        subject, service, data = backend(inspection, resolver=resolve)
        with pytest.raises(AcasIdentityVerificationError) as caught:
            await subject.acquire(KEY, SPEC)
        assert "sensitive" not in str(caught.value) and not service.labels
        assert not data[0].closed
        assert await subject.dispose(KEY) is None
        await subject.aclose()
        assert all(client.closed == client.credential.closed == 1 for client in data)

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_blocked_read_cancels_without_creation_and_shutdown_waits_for_lease(cancel):
    async def scenario():
        inspection = Inspection()
        started = asyncio.Event()
        release = asyncio.Event()

        async def wait():
            started.set()
            await release.wait()

        inspection.wait = wait
        subject, service, data = backend(
            inspection, resolver=resolve, timeout=0.02 if not cancel else 5
        )
        acquiring = asyncio.create_task(subject.acquire(KEY, SPEC))
        await started.wait()
        closing = asyncio.create_task(subject.aclose())
        await asyncio.sleep(0)
        assert not data[0].closed and not inspection.clients[0].closed
        if cancel:
            acquiring.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else AcasIdentityVerificationError):
            await acquiring
        await closing
        assert not service.labels and not subject._registry
        assert data[0].closed == inspection.clients[0].closed == data[0].credential.closed == 1

    asyncio.run(scenario())


def test_acquire_check_is_loop_owned_and_never_shared_between_loops():
    inspection = Inspection()
    subject, _, data = backend(inspection, resolver=resolve)

    async def use(number):
        key = replace(KEY, thread_id=str(number))
        await subject.acquire(key, SPEC)
        await subject.acquire(key, SPEC)
        # Each loop closes its idle entry before it stops, without closing the whole backend.
        state = subject._client_pool._loops[asyncio.get_running_loop()]
        await subject._client_pool._close_loop(asyncio.get_running_loop(), state)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(asyncio.run, use(n)) for n in range(2)]
        for future in futures:
            future.result(timeout=10)
    assert len({loop for _, _, loop in inspection.reads}) == 2
    assert len(inspection.clients) == len(data) == 2
    assert all(client.closed == client.credential.closed == 1 for client in data)
    asyncio.run(subject.aclose())


def test_attached_workload_is_refused_before_client_construction():
    subject = AcasSandboxBackend(_config())
    spec = SandboxSpec(
        kind="identity",
        requires=frozenset({Capability.ATTACHED_IDENTITY}),
        max_identity_scope=IdentityScope.SHARED,
        max_identity_retention_seconds=60,
    )
    with pytest.raises(SandboxCapabilityNotSupported, match="attached identity"):
        asyncio.run(subject.acquire(KEY, spec))
    assert not subject._client_pool._loops
    router = SandboxRouter([subject], max_identity_scope=IdentityScope.SHARED)
    with pytest.raises(SandboxCapabilityNotSupported):
        router.ensure_can_serve(spec)


def test_both_pipelines_close_and_retry_only_the_failed_one():
    async def scenario():
        attempts = [0, 0]

        async def close_data():
            attempts[0] += 1
            if attempts[0] == 1:
                raise RuntimeError("close failed")

        async def close_management():
            attempts[1] += 1

        clients = GroupClients(SimpleNamespace(close=close_data), lambda: None)
        clients.management = SimpleNamespace(close=close_management)
        with pytest.raises(AcasClientCloseError):
            await clients.close()
        assert attempts == [1, 1] and clients.management is None
        await clients.close()
        assert attempts == [2, 1] and clients.data is None

    asyncio.run(scenario())


def test_production_factory_owns_the_management_client_and_uses_the_sdk_model(monkeypatch):
    from azure.containerapps.sandbox.aio import SandboxGroupManagementClient

    async def scenario():
        management = []

        def create(**kwargs):
            client = SandboxGroupManagementClient(**kwargs)
            management.append(client)

            async def read(path):
                assert (
                    path
                    == "/subscriptions/subscription/resourceGroups/resource-group/providers/Microsoft.App/sandboxGroups/group"
                )
                return {"id": GROUP.id, "name": GROUP.name, "properties": GROUP.properties}

            client._arm_get = read
            return client

        monkeypatch.setattr("azure.containerapps.sandbox.aio.SandboxGroupManagementClient", create)
        subject = AcasSandboxBackend(_config())
        credential = _Credential()
        clients = subject._group_client(credential)
        assert clients.management is None
        _check_group(await clients.get_group("group"), subject._config)
        assert clients.management is management[0]
        assert clients.data._credential is management[0]._credential is credential
        await clients.close()
        assert clients.data is clients.management is None
        assert credential.closed == 0
        await credential.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [0, -1, True, float("inf"), float("nan"), "15"])
def test_identity_read_deadline_must_be_finite_and_positive(value):
    with pytest.raises(ValueError, match="identity_check_seconds"):
        _config(identity_check_seconds=value)
