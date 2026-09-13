"""A live instance keeps its execution contract across acquires and router resets."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from maf_sandbox import (
    Capability,
    Cleanup,
    DisposalFailure,
    FailedReclaimPolicy,
    ReclaimConfig,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    Selection,
)
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InProcessSandboxBackend

_KEY = SandboxKey(scope="scope", thread_id="thread", agent_dir="agent")
_SPEC = SandboxSpec(kind="codeact", execution_contract="python:exec")


def _router(*, snapshot=False, **kwargs):
    backend = InProcessSandboxBackend(
        declarations=replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=FAKE_BACKEND_DECLARATIONS.capabilities
            | ({Capability.SNAPSHOT} if snapshot else set()),
        )
    )
    return SandboxRouter([backend], min_isolation=backend.isolation, **kwargs), backend


@pytest.mark.parametrize("selection", list(Selection))
@pytest.mark.parametrize(
    "before, after",
    [(None, "python:run_code"), ("python:exec", "python:run_code"), ("python:run_code", None)],
)
def test_a_known_instance_cannot_change_contract_without_disposal(selection, before, after):
    router, backend = _router(selection=selection)
    initial, changed = (
        replace(_SPEC, execution_contract=before),
        replace(_SPEC, execution_contract=after),
    )

    async def scenario():
        held = await router.acquire(_KEY, initial)
        await held.write_file("keep", "original", working_directory=".")
        disposed = len(backend.disposed)
        with pytest.raises(ValueError, match="different execution contract"):
            await router.acquire(_KEY, changed)
        assert len(backend.disposed) == disposed
        assert await held.read_file("keep", working_directory=".", max_bytes=20) == b"original"
        assert await router.acquire(_KEY, initial) is held
        await router.dispose_kind(_KEY, "codeact", timeout=5)
        await router.acquire(_KEY, changed)

    asyncio.run(scenario())


def test_matching_contracts_allow_other_spec_changes():
    router, _ = _router()

    async def scenario():
        first = await router.acquire(_KEY, _SPEC)
        assert (
            await router.acquire(
                _KEY, replace(_SPEC, files_out=replace(_SPEC.files_out, max_files=1))
            )
            is first
        )

    asyncio.run(scenario())


def test_reset_carries_the_contract_to_the_new_instance_identity():
    router, _ = _router(snapshot=True, min_cleanup=Cleanup.RESET)

    async def scenario():
        admission = await router.enter_call(_KEY, _SPEC, owner="call")
        sandbox = await router.acquire(_KEY, _SPEC, _admission=admission)
        previous = sandbox.instance_id
        await router.finish_call(_KEY, _SPEC, admission=admission, owner="call", sandbox=sandbox)
        assert sandbox.instance_id != previous
        assert await router.acquire(_KEY, _SPEC) is sandbox
        with pytest.raises(ValueError, match="execution contract"):
            await router.acquire(_KEY, replace(_SPEC, execution_contract="python:run_code"))

    asyncio.run(scenario())


def test_an_external_replacement_may_have_a_new_contract():
    router, backend = _router(snapshot=True)

    async def scenario():
        await router.acquire(_KEY, _SPEC)
        backend.sandbox = type(backend.sandbox)()
        assert (
            await router.acquire(_KEY, replace(_SPEC, execution_contract="python:run_code"))
            is backend.sandbox
        )

    asyncio.run(scenario())


def test_failed_disposal_does_not_release_the_contract():
    router, backend = _router(reclaim=ReclaimConfig(failed_reclaim_policy=FailedReclaimPolicy.KEEP))

    async def scenario():
        await router.acquire(_KEY, _SPEC)
        backend.dispose_failure = DisposalFailure("unknown", "not removed")
        await router.dispose_kind(_KEY, "codeact", timeout=5)
        with pytest.raises(ValueError, match="execution contract"):
            await router.acquire(_KEY, replace(_SPEC, execution_contract="python:run_code"))

    asyncio.run(scenario())


def test_concurrent_acquires_cannot_bind_two_contracts_to_one_instance():
    router, _ = _router(snapshot=True)

    async def scenario():
        results = await asyncio.gather(
            router.acquire(_KEY, _SPEC),
            router.acquire(_KEY, replace(_SPEC, execution_contract="python:run_code")),
            return_exceptions=True,
        )
        assert sum(isinstance(result, ValueError) for result in results) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["dispose", "dispose_kind", "dispose_scope"])
def test_successful_disposal_forgets_the_contract(method):
    router, _ = _router()

    async def scenario():
        await router.acquire(_KEY, _SPEC)
        if method == "dispose_scope":
            await router.dispose_scope(_KEY.scope, _KEY.thread_id)
        elif method == "dispose_kind":
            await router.dispose_kind(_KEY, _SPEC.kind, timeout=5)
        else:
            await router.dispose(_KEY)
        await router.acquire(_KEY, replace(_SPEC, execution_contract="python:run_code"))

    asyncio.run(scenario())


@pytest.mark.parametrize("contract", ["", " ", 1, ()])
def test_invalid_execution_contracts_refuse_at_construction(contract):
    with pytest.raises(ValueError, match="execution_contract"):
        replace(_SPEC, execution_contract=contract)
