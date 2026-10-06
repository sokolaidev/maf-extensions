"""Measure whether a provider's prompt cache engages consistently.

The live benchmark reports a cost spread between repeats of the same strategy. On Chat
Completions routes that spread sat around 10%; on the Foundry Responses route it reached
121%, which is wider than the differences between the strategies being compared and makes
the cost ranking meaningless there.

Output length cannot explain a gap that size, so the suspect is the cache itself: if a
provider serves a cache hit on one call and misses on the next for byte-identical input,
cost swings by the full discount, which is 10x on these models.

This probe removes every other variable. One prompt, built once, sent unchanged N times in
sequence. Anything that moves is the provider.

Usage:
    python probe_cache_stability.py foundry:gpt-5.4-mini
    python probe_cache_stability.py openrouter:openai/gpt-5.6-luna --calls 12
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from collections.abc import Sequence

from agent_framework import Message

from maf_cachebench import (
    ProviderCaller,
    build_provider,
    parse_provider_selector,
    prompt_cache_key_options,
)
from maf_cachebench._run_identity import new_run_id
from maf_cachebench._transcripts import TRUE_CHARS_PER_TOKEN, filler_text


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        A parser for the cache-stability probe.
    """
    parser = argparse.ArgumentParser(
        description="Send one identical prompt repeatedly and watch the cache."
    )
    parser.add_argument("provider", help="Provider or provider:model.")
    parser.add_argument(
        "--calls", type=int, default=10, help="Identical calls to make. Default 10."
    )
    parser.add_argument(
        "--prompt-tokens",
        type=int,
        default=8_000,
        help="Prompt size in tokens. Well above Azure's 1,024-token minimum cacheable size.",
    )
    parser.add_argument(
        "--no-temperature", action="store_true", help="Omit temperature for models that reject it."
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    """Send the same prompt repeatedly and report how steady the cache is.

    Args:
        args: Parsed command line arguments.

    Returns:
        A process exit code.
    """
    provider, model_override = parse_provider_selector(args.provider)
    runtime = build_provider(
        provider,
        temperature=None if args.no_temperature else 0.0,
        response_max_tokens=16,
        model=model_override,
    )
    # Responses-API clients keep history server-side; pin it off so every call is judged on
    # the prompt it carries rather than on a conversation the service is holding for us.
    namespace = new_run_id()
    extra = dict(prompt_cache_key_options(provider, namespace))
    if getattr(runtime.client, "STORES_BY_DEFAULT", False):
        extra["store"] = False
    caller = ProviderCaller(runtime, extra_options=extra, request_timeout=300.0)

    body = filler_text(11, int(args.prompt_tokens * TRUE_CHARS_PER_TOKEN))
    messages = [
        Message(
            role="system",
            contents=[f"Probe {namespace}. You are a terse assistant. Reply with one word."],
        ),
        Message(role="user", contents=[f"{body}\n\nReply with the single word: acknowledged."]),
    ]

    print(
        f"model: {runtime.model}   prompt: ~{args.prompt_tokens:,} tokens   calls: {args.calls}\n"
    )
    print(f"{'call':>5}{'input':>9}{'cached':>9}{'hit%':>7}{'ms':>8}")
    hits: list[float] = []
    for index in range(1, args.calls + 1):
        outcome = await caller(messages)
        if outcome.error:
            print(f"{index:>5}  ERROR {outcome.error[:60]}")
            continue
        got = outcome.input_tokens or 0
        cached = outcome.cached_tokens
        if cached is None:
            print(f"{index:>5}{got:>9,}{'n/a':>9}{'n/a':>7}{outcome.latency_ms:>8.0f}")
            continue
        hit = cached / got if got else 0.0
        hits.append(hit)
        print(f"{index:>5}{got:>9,}{cached:>9,}{hit:>6.0%}{outcome.latency_ms:>8.0f}")

    if len(hits) < 2:
        print("\nNot enough usable calls to judge stability.")
        return 0

    # The first call cannot hit a cache that nothing has written yet, so it is reported but
    # excluded: counting it as a miss would make every provider look intermittent.
    warm = hits[1:]
    lo, hi = min(warm), max(warm)
    print(f"\nwarm calls (excluding the first): {len(warm)}")
    print(f"hit rate  min {lo:.0%}   median {statistics.median(warm):.0%}   max {hi:.0%}")
    print(f"spread    {hi - lo:.0%} of the prompt")
    misses = sum(1 for value in warm if value < 0.5)
    if misses:
        print(
            f"\nINTERMITTENT: {misses} of {len(warm)} warm calls served under half the prompt from cache."
        )
        print("Cost measured on this route swings by the full cache discount, so a cost ranking")
        print("between strategies is not meaningful here no matter how many repeats are run.")
    elif hi - lo > 0.1:
        print(f"\nUNSTEADY: warm hit rate moved {hi - lo:.0%} on byte-identical input.")
    else:
        print("\nSTEADY: the cache engaged consistently. Cost differences here can be trusted.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the probe.

    Args:
        argv: Argument vector. Defaults to ``sys.argv[1:]``.

    Returns:
        A process exit code.
    """
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
