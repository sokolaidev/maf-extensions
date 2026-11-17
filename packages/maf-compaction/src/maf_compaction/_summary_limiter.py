"""Share summary request capacity across conversation stacks on one event loop."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Any, cast

logger = logging.getLogger(__name__)


class SummaryRateLimited(Exception):
    """A summary request was rate limited and remains eligible for a later pass."""


@dataclass(frozen=True)
class SummaryRequestStats:
    """A count-only snapshot of a shared limiter, including requests still in flight."""

    concurrency_limit: int
    current_concurrency: int
    peak_concurrency: int
    requests_started: int
    requests_finished: int
    rate_limit_events: int
    cooldowns: int


def _retry_after(error: Exception) -> float:
    response = getattr(error, "response", None)
    headers: Any = getattr(error, "headers", None) or getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        return 0.0
    entries = cast(Mapping[object, object], headers)
    value = next(
        (str(value) for key, value in entries.items() if str(key).lower() == "retry-after"), ""
    )
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(str(value))
            seconds = (date - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0.0
    return max(0.0, seconds) if math.isfinite(seconds) else 0.0


def _classify_rate_limit(error: Exception) -> float | None:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        if (
            getattr(current, "status_code", None) == 429
            or getattr(response, "status_code", None) == 429
        ):
            return _retry_after(current) if isinstance(current, Exception) else 0.0
        current = current.__cause__ or current.__context__
    return None


class SummaryRequestLimiter:
    """Size shared concurrency from a deployment's host-assigned token allowance.

    Share one instance across stacks on the same event loop. Hosts divide the deployment
    allowance between processes. This estimates throughput; it is not a token bucket or an
    RPM limiter. Missing output caps conservatively restrict the shared limiter to one.
    """

    def __init__(
        self,
        summary_tokens_per_minute: int,
        *,
        expected_request_seconds: float = 40.0,
        max_parallel_summary_requests: int | None = None,
        rate_limit_cooldown_seconds: float = 90.0,
        classify_rate_limit: Callable[[Exception], float | None] | None = None,
    ) -> None:
        """Configure capacity and an optional classifier returning a wait in seconds or None."""
        if type(summary_tokens_per_minute) is not int or summary_tokens_per_minute <= 0:
            raise ValueError("summary_tokens_per_minute must be a positive integer.")
        if max_parallel_summary_requests is not None and (
            type(max_parallel_summary_requests) is not int or max_parallel_summary_requests <= 0
        ):
            raise ValueError("max_parallel_summary_requests must be a positive integer.")
        for name, value in (
            ("expected_request_seconds", expected_request_seconds),
            ("rate_limit_cooldown_seconds", rate_limit_cooldown_seconds),
        ):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite.")
        self.summary_tokens_per_minute = summary_tokens_per_minute
        self.max_parallel_summary_requests = max_parallel_summary_requests
        self.rate_limit_cooldown_seconds = rate_limit_cooldown_seconds
        self._duration = expected_request_seconds
        self._classify = classify_rate_limit or _classify_rate_limit
        self._tokens = 0
        self._uncapped = False
        self._reduced: int | None = None
        self._recover_at = 0.0
        self._retry_at = 0.0
        self._active = self._peak = self._started = self._finished = 0
        self._rate_limits = self._cooldowns = 0
        self._condition = asyncio.Condition()

    def configure_request(self, input_limit: int, options: Mapping[str, Any]) -> int:
        """Register a request ceiling and return the shared concurrency limit.

        The largest registered input-plus-output ceiling sizes all stacks conservatively.
        """
        if type(input_limit) is not int or input_limit <= 0:
            raise ValueError("input_limit must be a positive integer.")
        cap = options.get("max_tokens")
        if type(cap) is not int or cap <= 0:
            self._uncapped = True
        else:
            self._tokens = max(self._tokens, input_limit + cap)
        return self.concurrency_limit

    @property
    def concurrency_limit(self) -> int:
        """Current admission limit; a reduction lets existing requests finish."""
        target = (
            1
            if self._uncapped or not self._tokens
            else max(
                1, math.floor(self.summary_tokens_per_minute * self._duration / (60 * self._tokens))
            )
        )
        if self.max_parallel_summary_requests is not None:
            target = min(target, self.max_parallel_summary_requests)
        if self._reduced is None:
            return target
        now = monotonic()
        if now >= self._recover_at:
            self._reduced += 1
            self._recover_at = now + self.rate_limit_cooldown_seconds
            if self._reduced >= target:
                self._reduced = None
                return target
        return min(target, self._reduced)

    @property
    def stats(self) -> SummaryRequestStats:
        """Return counters without request text or provider identifiers."""
        return SummaryRequestStats(
            self.concurrency_limit,
            self._active,
            self._peak,
            self._started,
            self._finished,
            self._rate_limits,
            self._cooldowns,
        )

    @asynccontextmanager
    async def request(
        self, before_request: Callable[[], None] | None = None
    ) -> AsyncGenerator[None]:
        """Admit one request and account for success, failure or cancellation."""
        async with self._condition:
            while True:
                if before_request is not None:
                    before_request()
                if self._active < self.concurrency_limit and monotonic() >= self._retry_at:
                    break
                # Also recheck admission deadlines while other stacks hold capacity.
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=0.1)
                except TimeoutError:
                    pass
            self._active += 1
            self._started += 1
            self._peak = max(self._peak, self._active)
        started = monotonic()
        try:
            yield
        except Exception as error:
            wait = self._classify(error)
            if wait is None:
                raise
            if isinstance(wait, bool) or not math.isfinite(wait) or wait < 0:
                raise ValueError(
                    "classify_rate_limit must return non-negative finite seconds or None."
                ) from error
            now = monotonic()
            self._reduced = max(1, self.concurrency_limit // 2)
            self._rate_limits += 1
            self._cooldowns += 1
            self._recover_at = max(
                self._recover_at, now + max(self.rate_limit_cooldown_seconds, wait)
            )
            self._retry_at = max(self._retry_at, now + wait)
            logger.warning(
                "Summary rate limit: concurrency=%d active=%d events=%d cooldown_seconds=%.3f.",
                self._reduced,
                self._active,
                self._rate_limits,
                self._recover_at - now,
            )
            raise SummaryRateLimited from error
        else:
            self._duration = 0.8 * self._duration + 0.2 * max(0.001, monotonic() - started)
        finally:
            async with self._condition:
                self._active -= 1
                self._finished += 1
                self._condition.notify_all()
