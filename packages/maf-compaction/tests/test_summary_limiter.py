"""Deployment-wide summary capacity, rate-limit recovery and request accounting."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from maf_compaction import SummaryRequestLimiter
from maf_compaction import _summary_limiter as module
from maf_compaction._summary_limiter import SummaryRateLimited

pytestmark = pytest.mark.anyio


class RateLimit(Exception):
    status_code = 429

    def __init__(self, retry_after=None):
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(module, "monotonic", clock)
    return clock


@pytest.mark.parametrize(
    "name,value",
    [
        ("summary_tokens_per_minute", 0),
        ("summary_tokens_per_minute", True),
        ("summary_tokens_per_minute", 1.5),
        ("max_parallel_summary_requests", 0),
        ("max_parallel_summary_requests", True),
        ("expected_request_seconds", 0),
        ("expected_request_seconds", float("nan")),
        ("rate_limit_cooldown_seconds", float("inf")),
    ],
)
def test_configuration_validation(name, value):
    with pytest.raises(ValueError, match=name):
        SummaryRequestLimiter(**{"summary_tokens_per_minute": 300_000, name: value})


async def test_derived_capacity_duration_average_and_largest_request(clock):
    limiter = SummaryRequestLimiter(300_000)
    assert limiter.configure_request(12_000, {"max_tokens": 4_000}) == 12
    async with limiter.request():
        clock.now += 20
    assert limiter.concurrency_limit == 11  # EMA duration: 36 seconds.
    assert limiter.configure_request(26_000, {"max_tokens": 4_000}) == 6
    assert limiter.configure_request(12_000, {"max_tokens": 4_000}) == 6
    assert limiter.configure_request(12_000, {}) == 1


async def test_shared_capacity_never_exceeded_and_cancelled_waiters_release_nothing(clock):
    limiter = SummaryRequestLimiter(300_000, max_parallel_summary_requests=3)
    limiter.configure_request(12_000, {"max_tokens": 4_000})
    release = asyncio.Event()
    full = asyncio.Event()

    async def request():
        async with limiter.request():
            if limiter.stats.current_concurrency == 3:
                full.set()
            await release.wait()

    tasks = [asyncio.create_task(request()) for _ in range(9)]
    await asyncio.wait_for(full.wait(), 2)
    tasks[-1].cancel()
    await asyncio.gather(tasks[-1], return_exceptions=True)
    assert limiter.stats.current_concurrency == limiter.stats.peak_concurrency == 3
    assert limiter.stats.requests_started == 3
    release.set()
    await asyncio.gather(*tasks[:-1])
    assert limiter.stats.requests_started == limiter.stats.requests_finished == 8
    assert limiter.stats.current_concurrency == 0
    assert limiter.stats.peak_concurrency == 3


async def test_429_cause_retry_after_repeated_cooldown_and_stepwise_recovery(clock):
    limiter = SummaryRequestLimiter(300_000)
    limiter.configure_request(12_000, {"max_tokens": 4_000})
    with pytest.raises(SummaryRateLimited):
        async with limiter.request():
            try:
                raise RateLimit("120")
            except RateLimit as error:
                raise RuntimeError("wrapper") from error
    assert limiter.concurrency_limit == 6
    clock.now += 119
    assert limiter.concurrency_limit == 6
    clock.now += 1
    assert limiter.concurrency_limit == 7
    assert limiter.concurrency_limit == 7
    with pytest.raises(SummaryRateLimited):
        async with limiter.request():
            raise RateLimit()
    assert limiter.concurrency_limit == 3
    clock.now += 89
    assert limiter.concurrency_limit == 3
    clock.now += 1
    assert limiter.concurrency_limit == 4
    for expected in range(5, 13):
        clock.now += 90
        assert limiter.concurrency_limit == expected
    assert limiter.stats.rate_limit_events == limiter.stats.cooldowns == 2
    assert limiter.stats.requests_finished == 2


async def test_retry_after_blocks_admission_until_provider_wait_expires(clock):
    limiter = SummaryRequestLimiter(300_000)
    limiter.configure_request(12_000, {"max_tokens": 4_000})
    with pytest.raises(SummaryRateLimited):
        async with limiter.request():
            raise RateLimit("120")
    entered = asyncio.Event()

    async def request():
        async with limiter.request():
            entered.set()

    task = asyncio.create_task(request())
    await asyncio.sleep(0)
    assert not entered.is_set()
    clock.now += 120
    await asyncio.wait_for(task, 2)
    assert entered.is_set()


@pytest.mark.parametrize("header", ["invalid", "nan", "inf", "-1"])
async def test_invalid_retry_after_still_uses_default_cooldown(clock, header):
    limiter = SummaryRequestLimiter(300_000)
    limiter.configure_request(12_000, {"max_tokens": 4_000})
    with pytest.raises(SummaryRateLimited):
        async with limiter.request():
            raise RateLimit(header)
    clock.now += 89
    assert limiter.concurrency_limit == 6
    clock.now += 1
    assert limiter.concurrency_limit == 7


def test_http_date_and_response_status_are_provider_independent():
    class Response:
        status_code = 429
        headers = {"retry-after": format_datetime(datetime.now(UTC) + timedelta(seconds=150))}

    class Error(RuntimeError):
        response = Response()

    wait = module._classify_rate_limit(Error())
    assert wait is not None
    assert 148 <= wait <= 150


async def test_custom_classifier_and_ordinary_failure(clock):
    limiter = SummaryRequestLimiter(
        300_000, classify_rate_limit=lambda error: 180 if isinstance(error, LookupError) else None
    )
    limiter.configure_request(12_000, {"max_tokens": 4_000})
    with pytest.raises(ValueError):
        async with limiter.request():
            raise ValueError("ordinary failure")
    assert limiter.stats.rate_limit_events == 0
    with pytest.raises(SummaryRateLimited):
        async with limiter.request():
            raise LookupError()
    clock.now += 179
    assert limiter.concurrency_limit == 6
    clock.now += 1
    assert limiter.concurrency_limit == 7


async def test_deadline_rechecked_while_waiting(clock):
    limiter = SummaryRequestLimiter(300_000, max_parallel_summary_requests=1)
    limiter.configure_request(12_000, {"max_tokens": 4_000})

    def check():
        if clock.now > 100:
            raise TimeoutError("admission stopped")

    async def request():
        async with limiter.request(check):
            pytest.fail("A waiting request was admitted after its deadline")

    async with limiter.request():
        task = asyncio.create_task(request())
        await asyncio.sleep(0)
        clock.now += 1
        with pytest.raises(TimeoutError, match="admission stopped"):
            await asyncio.wait_for(task, 2)
    assert limiter.stats.requests_started == limiter.stats.requests_finished == 1
