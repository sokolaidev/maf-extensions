"""Compare sequential and parallel merge phases using identical synthetic model answers.

Run this file directly from the repository environment. No provider is contacted.
"""

from __future__ import annotations

import asyncio
from time import monotonic

from test_record_budget import TOKENIZER, _history, _sent_values, _strategy, _Summarizer

from maf_compaction import SummaryRequestLimiter
from maf_compaction._composed import _ChainTimeSpent


async def measure(parallel: int) -> dict[str, int | float]:
    """Measure a long record phase with a request longer than each admission window."""

    class Delayed(_Summarizer):
        input_tokens = 0

        async def get_response(self, messages, **kwargs):
            self.input_tokens += sum(
                TOKENIZER.count_tokens(message.text or "") for message in messages
            )
            await asyncio.sleep(0.05)
            return await super().get_response(messages, **kwargs)

    model = Delayed()
    strategy = _strategy(model, budget=4_000, share=1.0)
    strategy.user_turns.max_summary_input_tokens = 12_000
    if parallel > 1:
        strategy.user_turns.summary_limiter = SummaryRequestLimiter(
            100_000_000, expected_request_seconds=0.05, max_parallel_summary_requests=parallel
        )
    messages = _history(60)
    values = _sent_values(messages)
    over_budget = passes = 0
    started = monotonic()
    while strategy._record_tokens(messages) > 4_000:
        passes += 1
        if passes > 100:
            raise RuntimeError("The synthetic merge phase made insufficient progress")
        token = strategy._chain_deadline.set(monotonic() + 0.03)
        try:
            await strategy._merge_batches(messages, None, 1.0)
        except _ChainTimeSpent:
            pass
        finally:
            strategy._chain_deadline.reset(token)
        over_budget += int(strategy._record_tokens(messages) > 4_000)
    if _sent_values(messages) != values:
        raise AssertionError("The synthetic answers lost source values")
    return {
        "parallel": parallel,
        "seconds": round(monotonic() - started, 3),
        "passes": passes,
        "over_budget_passes": over_budget,
        "requests": len(model.requests),
        "input_tokens": model.input_tokens,
        "values_retained": len(values),
        "peak_concurrency": strategy.user_turns.summary_limiter.stats.peak_concurrency
        if strategy.user_turns.summary_limiter
        else 1,
    }


async def main() -> None:
    """Print three quick pairs; these are harness timings, not provider measurements."""
    for _ in range(3):
        for parallel in (1, 3):
            print(await measure(parallel))


if __name__ == "__main__":
    asyncio.run(main())
