"""Client-pool progress and request lifetime across task scheduling policies."""

from __future__ import annotations

import asyncio
import contextvars
import gc
import subprocess
import sys
import weakref

import pytest
from test_acas_credentials import Client, Credential, binding, pool

from maf_sandbox_acas import AcasCredentialBinding, AcasCredentialError
from maf_sandbox_acas._credentials import ClientPool


class _Credential(Credential):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args, **kwargs):
        await self.close()


async def _eager_case(case):
    asyncio.get_running_loop().set_task_factory(asyncio.eager_task_factory)
    clients = pool(capacity=1)
    created = []

    def credential():
        if case == "construction":
            assert clients._guard.acquire(blocking=False), "factory ran under the pool guard"
            clients._guard.release()
        result = _Credential()
        created.append(result)
        return result

    async with clients.lease(AcasCredentialBinding("first", "1", credential)) as first:
        pass
    async with clients.lease(AcasCredentialBinding("second", "1", credential)) as second:
        assert first.closed == first.credential.closed == 1
    await clients.aclose()
    assert second.closed == second.credential.closed == 1
    assert len(created) == 2 and not clients._loops


@pytest.mark.parametrize("case", ["construction", "eviction"])
def test_eager_tasks_complete_construction_eviction_and_shutdown(case):
    # A blocked event-loop thread cannot enforce an asyncio timeout.
    result = subprocess.run(
        [sys.executable, __file__, case],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("outcome", ["timeout", "cancel"])
def test_completed_capacity_waiters_release_context_before_capacity_changes(outcome):
    class Request:
        pass

    request_context = contextvars.ContextVar[Request]("request_context")
    references = []

    async def scenario():
        clients = ClientPool(
            Client, capacity=1, wait_seconds=0.01 if outcome == "timeout" else 1, close_seconds=1
        )

        async def attempt():
            request = Request()
            references.append(weakref.ref(request))
            token = request_context.set(request)
            try:
                async with clients.lease(binding("blocked")):
                    pytest.fail("capacity exceeded")
            except AcasCredentialError:
                assert outcome == "timeout"
            except asyncio.CancelledError:
                assert outcome == "cancel"
            finally:
                request_context.reset(token)

        async def use():
            async with clients.lease(binding("survivor")) as client:
                return client

        try:
            async with clients.lease(binding("active")) as active:
                for _ in range(2):
                    requests = [asyncio.create_task(attempt()) for _ in range(20)]
                    if outcome == "cancel":
                        await asyncio.sleep(0)
                        for request in requests:
                            request.cancel()
                    await asyncio.gather(*requests)
                    await asyncio.sleep(0)
                    gc.collect()
                    assert all(reference() is None for reference in references)
                    assert not active.closed
                    state = clients._loops[asyncio.get_running_loop()]
                    assert len(getattr(state.changed, "_done_callbacks")) == 1
                survivor = asyncio.create_task(use())
                await asyncio.sleep(0)
                assert not survivor.done()
            assert isinstance(await survivor, Client)
        finally:
            await clients.aclose()

    asyncio.run(scenario())


if __name__ == "__main__":
    asyncio.run(_eager_case(sys.argv[1]))
