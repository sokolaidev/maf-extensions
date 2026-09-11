"""Service label discovery owns ACAS disposal selection across backend instances."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from maf_sandbox import Egress, SandboxKey, SandboxSpec
from maf_sandbox.conformance import assert_instance_disposal_conformance

from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig
from maf_sandbox_acas._backend import _Held, _sandbox_labels

KEY = SandboxKey(scope="scope", thread_id="thread", agent_dir="agent")
SPEC = SandboxSpec(kind="work")


class _Service:
    def __init__(self):
        self.rows = {}
        self.deleted = []
        self.failure: str | None = None

    def add(self, identity, key=KEY, spec=SPEC):
        self.rows[identity] = _sandbox_labels(key, spec)

    async def list_sandboxes(self, *, labels):
        if self.failure == "list":
            raise RuntimeError("listing failed")
        for identity, owned in list(self.rows.items()):
            if all(owned.get(label) == value for label, value in labels.items()):
                yield SimpleNamespace(id=identity, labels=owned)

    def get_sandbox_client(self, identity):
        async def delete():
            if self.failure == "cancel":
                raise asyncio.CancelledError
            if self.failure == "delete":
                raise RuntimeError("delete failed")
            self.deleted.append(identity)
            self.rows.pop(identity, None)

        async def begin_delete():
            return SimpleNamespace(result=delete)

        return SimpleNamespace(begin_delete=begin_delete)


def _backend(service):
    backend = AcasSandboxBackend(AcasSandboxConfig(endpoint="https://sandbox.invalid"))
    backend._group_client = lambda: service
    return backend


def test_another_backend_instance_discovers_exact_id_and_preserves_siblings():
    service = _Service()
    for identity in ("target", "same-kind"):
        service.add(identity)
    service.add("other-kind", spec=replace(SPEC, kind="other"))
    backend = _backend(service)

    async def exists(identity):
        return identity in service.rows

    asyncio.run(
        assert_instance_disposal_conformance(
            backend,
            KEY,
            SPEC.kind,
            "target",
            ["same-kind", "other-kind"],
            exists,
        )
    )
    service.add("replacement")
    assert asyncio.run(backend.dispose(KEY, kind=SPEC.kind, instance_id="target")) is None
    assert set(service.rows) == {"same-kind", "other-kind", "replacement"}


@pytest.mark.parametrize("kind", [None, SPEC.kind])
def test_service_sweep_reaches_every_variant_without_a_local_registry(kind):
    service = _Service()
    service.add("one")
    service.add("two")
    service.add("sibling", spec=replace(SPEC, kind="other"))
    service.add("foreign", key=replace(KEY, agent_dir="other"))
    assert asyncio.run(_backend(service).dispose(KEY, kind=kind)) is None
    assert set(service.rows) == ({"foreign"} if kind is None else {"foreign", "sibling"})


@pytest.mark.parametrize("field", ["scope", "thread_id", "agent_dir", "kind", "call_id"])
def test_instance_selector_cannot_cross_ownership(field):
    service = _Service()
    service.add("target")
    key = KEY if field == "kind" else replace(KEY, **{field: "other"})
    kind = "other" if field == "kind" else SPEC.kind
    assert asyncio.run(_backend(service).dispose(key, kind=kind, instance_id="target")) is None
    assert not service.deleted


@pytest.mark.parametrize("failure", ["list", "delete", "cancel"])
def test_failures_retain_exact_ids_and_retry_without_deleting_replacements(failure):
    service = _Service()
    service.add("target")
    service.add("sibling", spec=replace(SPEC, kind="other"))
    backend = _backend(service)
    backend._registry[(KEY.scope, KEY.thread_id, KEY.agent_dir, KEY.call_id, SPEC.kind)] = _Held(
        "target", egress=(Egress.CLOSED, frozenset())
    )
    service.failure = failure
    pending = backend.dispose(KEY, kind=SPEC.kind, instance_id="target")
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(pending)
    else:
        assert asyncio.run(pending) is not None
    service.rows.pop("target")
    service.add("replacement")
    service.failure = None
    assert asyncio.run(backend.dispose(KEY, kind=SPEC.kind, instance_id="target")) is None
    assert set(service.rows) == {"replacement", "sibling"}
    assert not backend._undeleted


@pytest.mark.parametrize("label", ["scope", "thread", "agent", "kind", "call"])
def test_reserved_labels_are_refused_before_acquire_reaches_the_service(label):
    service = _Service()
    backend = _backend(service)
    with pytest.raises(ValueError, match=f"reserved sandbox labels: {label}"):
        asyncio.run(backend.acquire(KEY, replace(SPEC, labels={label: "foreign"})))


def test_failed_discovery_never_certifies_an_empty_registry():
    service = _Service()
    service.add("target")
    service.failure = "list"
    backend = _backend(service)
    failure = asyncio.run(backend.dispose(KEY))
    assert failure is not None and failure.code == "unlisted"
    service.failure = None
    assert asyncio.run(backend.dispose(KEY)) is None
    assert not service.rows
