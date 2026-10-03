"""Acquire freshness must describe one returned wrapper, never a retained instance."""

import asyncio
from types import SimpleNamespace

import pytest

from maf_sandbox.conformance import assert_fresh_acquire_conformance


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("valid", None),
        ("missing", "new instance did not report fresh"),
        ("truthy", "new instance did not report fresh"),
        ("replacement", "did not resume"),
        ("stale", "resumed instance reported fresh"),
        ("shared", "changed the first wrapper"),
    ],
)
def test_freshness_contract_rejects_incorrect_reports(mode, message):
    first = SimpleNamespace(instance_id="one", freshly_created=True)
    if mode == "missing":
        del first.freshly_created
    elif mode == "truthy":
        first.freshly_created = 1

    async def reacquire():
        if mode == "shared":
            first.freshly_created = False
            return first
        return SimpleNamespace(
            instance_id="two" if mode == "replacement" else "one",
            freshly_created=mode == "stale",
        )

    async def scenario():
        if message is None:
            await assert_fresh_acquire_conformance(first, reacquire)
        else:
            with pytest.raises(AssertionError, match=message):
                await assert_fresh_acquire_conformance(first, reacquire)

    asyncio.run(scenario())
