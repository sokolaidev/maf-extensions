"""Observe host-side ACAS throttle sleeps without changing the SDK's retry decisions."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, override

from azure.core.pipeline import PipelineResponse
from azure.core.pipeline.policies import AsyncRetryPolicy
from azure.core.pipeline.transport import AsyncHttpTransport

_retry_after_interrupted: ContextVar[bool] = ContextVar(
    "acas_retry_after_interrupted", default=False
)


class _ObservedRetryPolicy(AsyncRetryPolicy[Any, Any]):
    """Record a deadline that interrupts an HTTP 429 ``Retry-After`` sleep."""

    @override
    async def _sleep_for_retry(
        self, response: PipelineResponse[Any, Any], transport: AsyncHttpTransport[Any, Any]
    ) -> bool:
        if response.http_response.status_code != 429:
            return await super()._sleep_for_retry(response, transport)
        retry_after = self.get_retry_after(response)
        if retry_after:
            try:
                await transport.sleep(retry_after)
            except asyncio.CancelledError:
                _retry_after_interrupted.set(True)
                raise
            return True
        return False


def install_retry_observer(client: Any) -> None:
    """Replace the SDK's internal retry node while preserving its configuration and links.

    The preview client exposes no policy hook and shares this pipeline with its sandbox clients.
    """
    pipeline = client._pipeline
    policies = pipeline._impl_policies
    for index, policy in enumerate(policies):
        if isinstance(policy, _ObservedRetryPolicy):
            return
        if isinstance(policy, AsyncRetryPolicy):
            observed = _ObservedRetryPolicy()
            attributes: dict[str, Any] = vars(policy)
            vars(observed).update(attributes)
            policies[index] = observed
            if index:
                policies[index - 1].next = observed
            return
    raise RuntimeError("the ACAS SDK pipeline has no async retry policy")


@contextmanager
def retry_observation() -> Generator[None]:
    """Give one exec capture a task-local observation slot."""
    token = _retry_after_interrupted.set(False)
    try:
        yield
    finally:
        _retry_after_interrupted.reset(token)


def retry_after_interrupted() -> bool:
    """Whether this exec deadline interrupted an HTTP 429 ``Retry-After`` sleep."""
    return _retry_after_interrupted.get()
