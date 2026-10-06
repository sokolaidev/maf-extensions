"""Command line entry point for the compaction / prompt-cache benchmark."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agent_framework import Message, TokenizerProtocol

from ._cli_selection import (
    replay_strategy_names,
    validate_summarizer_selector,
    validate_unique_selection,
)
from ._metrics import summarize_cell
from ._providers import (
    PROVIDER_SPECS,
    ProviderRuntime,
    build_provider,
    parse_provider_selector,
    prompt_cache_key_options,
    provider_names,
)
from ._report import render_summary_table, write_summary_csv
from ._run_identity import new_run_id
from ._runner import CallOutcome, ProviderCaller, run_cell
from ._strategies import (
    StrategyOptions,
    build_strategy,
    needs_summarizer,
    resolve_context_window,
)
from ._tokenizers import TOKENIZER_NAMES, build_tokenizer
from ._transcripts import TRANSCRIPT_PRESETS, build_preset
from ._types import CellKey, CellSummary, TurnRecord

__all__ = ["build_parser", "main", "run_benchmark"]

_DEFAULT_STRATEGIES = "none,context_window,truncation,tool_result"


class _DryRunCaller:
    """Stands in for a provider so the full matrix can be validated for free."""

    async def __call__(self, messages: Sequence[Message]) -> CallOutcome:
        """Return an empty outcome without contacting any provider."""
        return CallOutcome(latency_ms=0.0)


def _split(value: str) -> list[str]:
    """Split a comma-separated option value into non-empty entries."""
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        A parser covering provider, strategy, and size selection plus output options.
    """
    parser = argparse.ArgumentParser(
        prog="cachebench",
        description=(
            "Measure how Agent Framework compaction strategies interact with provider "
            "prompt caching, by replaying deterministic scripts with isolated per-cell prefixes."
        ),
    )
    parser.add_argument(
        "--providers",
        default="azure",
        help=(
            "Comma-separated, each 'provider' or 'provider:model' to compare models on one "
            f"provider. Available: {','.join(provider_names())}"
        ),
    )
    parser.add_argument(
        "--strategies",
        default=_DEFAULT_STRATEGIES,
        help=f"Comma-separated. Available: {','.join(replay_strategy_names())}",
    )
    parser.add_argument(
        "--sizes",
        default="mid",
        help=f"Comma-separated transcript presets. Available: {','.join(TRANSCRIPT_PRESETS)}",
    )
    parser.add_argument(
        "--repeats", type=int, default=1, help="Independent replays per cell. Default 1."
    )
    parser.add_argument(
        "--response-max-tokens",
        type=int,
        default=16,
        help="Cap on generated tokens. Output is discarded, so keep this small. Default 16.",
    )
    parser.add_argument(
        "--temperature", type=float, default=0.0, help="Sampling temperature. Default 0.0."
    )
    parser.add_argument(
        "--no-temperature",
        action="store_true",
        help="Omit temperature entirely, for models that reject the parameter.",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=None,
        help=(
            "Simulated context window driving compaction budgets. Defaults to 60%% of each "
            "transcript's size."
        ),
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=512,
        help=(
            "The model's maximum output tokens per response, which is subtracted from "
            "--context-window to give the input budget every threshold is a fraction of. "
            "This is the model's ceiling, not the size of reply you want: setting it too low "
            "inflates the budget and can push a strategy's trigger above what the service "
            "will accept, which disables compaction with no warning."
        ),
    )
    parser.add_argument(
        "--tokenizer",
        default="estimator",
        choices=list(TOKENIZER_NAMES),
        help=(
            "Counter for compaction budgets and the prefix oracle. 'estimator' is fast but "
            "runs ~2x a real BPE count; use 'tiktoken' whenever thresholds must land on real "
            "token values. Default estimator."
        ),
    )
    parser.add_argument(
        "--keep-last-groups", type=int, default=6, help="Groups kept by sliding_window."
    )
    parser.add_argument(
        "--keep-tool-groups", type=int, default=2, help="Tool-call groups kept verbatim."
    )
    parser.add_argument(
        "--cache-read-ratio",
        type=float,
        default=0.25,
        help=(
            "Price of a cached input token relative to a fresh one, for the cost column. Default "
            "0.25."
        ),
    )
    parser.add_argument(
        "--no-cost", action="store_true", help="Omit the effective-input-token column."
    )
    parser.add_argument(
        "--prompt-cache-key",
        action="store_true",
        help=(
            "Send a per-cell prompt_cache_key. Off by default: every provider measured so "
            "far caches without one, and an older deployment can reject the unknown field."
        ),
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=300.0,
        help="Seconds a single call may take before it is abandoned. 0 disables. Default 300.",
    )
    parser.add_argument(
        "--turn-delay",
        type=float,
        default=0.0,
        help="Seconds between turns, for strict rate limits.",
    )
    parser.add_argument(
        "--summarizer-provider",
        default=None,
        help="Provider used as the summarizer client for the 'summarization' strategy.",
    )
    parser.add_argument(
        "--out", type=Path, default=Path("cachebench-results"), help="Output directory."
    )
    parser.add_argument("--run-id", default=None, help="Run identifier. Defaults to a timestamp.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run the full matrix locally with no API calls. Reports prompt sizes and prefix reuse "
            "only."
        ),
    )
    return parser


def _validate_selection(name: str, selected: Sequence[str], known: Sequence[str]) -> None:
    """Raise when a selection contains unknown entries.

    Raises:
        SystemExit: If any selected entry is not known.
    """
    if not selected:
        raise SystemExit(f"At least one {name} is required.")
    unknown = [entry for entry in selected if entry not in known]
    if unknown:
        raise SystemExit(f"Unknown {name}: {', '.join(unknown)}. Available: {', '.join(known)}")


def _print_preflight(
    providers: Sequence[str],
    sizes: Sequence[str],
    strategies: Sequence[str],
    repeats: int,
) -> None:
    """Print the planned matrix and each provider's cache-reporting capability."""
    cells = len(providers) * len(sizes) * len(strategies) * repeats
    calls = (
        sum(TRANSCRIPT_PRESETS[size].turns for size in sizes)
        * len(providers)
        * len(strategies)
        * repeats
    )
    print(
        f"Matrix: {len(providers)} providers x {len(sizes)} sizes x {len(strategies)} "
        f"strategies x {repeats} repeats"
    )
    print(f"        {cells} cells, {calls} API calls")
    print("Provider cache reporting:")
    for selector in providers:
        spec = PROVIDER_SPECS[parse_provider_selector(selector)[0]]
        print(f"  {selector:<34} {spec.cache_reporting:<8} {spec.notes}")
    print()


async def _run_matrix(
    args: argparse.Namespace,
    *,
    tokenizer: TokenizerProtocol,
    providers: Sequence[str],
    sizes: Sequence[str],
    strategies: Sequence[str],
    on_record: Any,
) -> tuple[list[TurnRecord], list[CellSummary]]:
    """Execute every cell in the matrix sequentially."""
    run_id: str = args.run_id
    runtimes: dict[str, ProviderRuntime] = {}
    summarizer: Any = None
    if args.summarizer_provider is not None and not args.dry_run:
        summarizer_provider, summarizer_model = parse_provider_selector(args.summarizer_provider)
        summarizer = build_provider(
            summarizer_provider,
            temperature=None if args.no_temperature else args.temperature,
            response_max_tokens=512,
            model=summarizer_model,
        ).client

    all_records: list[TurnRecord] = []
    summaries: list[CellSummary] = []

    identities: dict[tuple[str, str], str] = {}
    resolved: list[tuple[str, str, str]] = []
    for selector in providers:
        provider, model_override = parse_provider_selector(selector)
        model = model_override or "dry-run"
        if not args.dry_run:
            try:
                runtimes[selector] = build_provider(
                    provider,
                    temperature=None if args.no_temperature else args.temperature,
                    response_max_tokens=args.response_max_tokens,
                    model=model_override,
                )
            except Exception as exc:
                print(f"!! skipping {selector}: {type(exc).__name__}: {exc}", flush=True)
                continue
            model = runtimes[selector].model
        identity = (provider, model)
        if identity in identities:
            raise SystemExit(
                f"Duplicate resolved provider/model: {identities[identity]!r} and {selector!r}."
            )
        identities[identity] = selector
        resolved.append((selector, provider, model))

    for selector, provider, model in resolved:
        for size in sizes:
            for strategy_name in strategies:
                for repeat in range(1, args.repeats + 1):
                    cell = CellKey(
                        provider=provider,
                        model=model,
                        transcript=size,
                        strategy=strategy_name,
                        repeat=repeat,
                    )
                    # A unique salt per cell gives each cell its own cache namespace, so
                    # cells cannot serve each other cache hits and contaminate results.
                    salt = f"{run_id}-{cell.label}-{model}"
                    transcript = build_preset(size, salt=salt, tokenizer=tokenizer)
                    options = StrategyOptions(
                        tokenizer=tokenizer,
                        max_context_window_tokens=resolve_context_window(
                            transcript.approx_final_prompt_tokens,
                            override=args.context_window,
                            max_output_tokens=args.max_output_tokens,
                        ),
                        max_output_tokens=args.max_output_tokens,
                        keep_last_groups=args.keep_last_groups,
                        keep_last_tool_call_groups=args.keep_tool_groups,
                        summarizer=summarizer,
                    )
                    strategy = build_strategy(strategy_name, options)

                    # Built per cell, not per provider: the cache key must be stable for
                    # every turn of a cell and different between cells. The underlying
                    # client is shared, so this costs nothing.
                    caller: Any = _DryRunCaller()
                    if not args.dry_run:
                        caller = ProviderCaller(
                            runtimes[selector],
                            extra_options=prompt_cache_key_options(
                                provider, salt, enable_optional=args.prompt_cache_key
                            ),
                            request_timeout=args.request_timeout or None,
                        )

                    print(f"-> {cell.label} ({len(transcript.turns)} turns)", flush=True)
                    records = await run_cell(
                        cell=cell,
                        transcript=transcript,
                        strategy=strategy,
                        tokenizer=tokenizer,
                        caller=caller,
                        on_record=on_record,
                        turn_delay=args.turn_delay,
                    )
                    all_records.extend(records)
                    summaries.append(
                        summarize_cell(
                            records,
                            cell=cell,
                            reports_cache_tokens=any(
                                record.cached_tokens is not None for record in records
                            ),
                        )
                    )
    return all_records, summaries


async def run_benchmark(args: argparse.Namespace) -> int:
    """Run the benchmark described by parsed arguments.

    Args:
        args: Parsed command line arguments.

    Returns:
        A process exit code.
    """
    if not math.isfinite(args.cache_read_ratio) or args.cache_read_ratio < 0:
        raise SystemExit("--cache-read-ratio must be finite and non-negative.")
    providers = _split(args.providers)
    strategies = _split(args.strategies)
    sizes = _split(args.sizes)
    for name, selection in (("provider", providers), ("strategy", strategies), ("size", sizes)):
        validate_unique_selection(name, selection)
    validate_summarizer_selector(args.summarizer_provider)
    _validate_selection(
        "provider", [parse_provider_selector(p)[0] for p in providers], provider_names()
    )
    _validate_selection("strategy", strategies, replay_strategy_names())
    _validate_selection("size", sizes, list(TRANSCRIPT_PRESETS))
    if args.repeats <= 0:
        raise SystemExit("--repeats must be greater than 0.")
    if needs_summarizer(strategies):
        if args.dry_run:
            raise SystemExit(
                "Summarization strategies require provider calls; omit them for --dry-run."
            )
        if args.summarizer_provider is None:
            raise SystemExit("Summarization strategies require --summarizer-provider.")

    args.run_id = args.run_id or new_run_id()
    tokenizer = build_tokenizer(args.tokenizer)
    _print_preflight(providers, sizes, strategies, args.repeats)

    # Stream every turn to disk as it completes. A long sweep is hours of paid API calls,
    # and buffering it all in memory until the end means a hang or a kill loses the lot.
    records_path = args.out / f"{args.run_id}-records.jsonl"
    records_path.parent.mkdir(parents=True, exist_ok=True)
    with records_path.open("w", encoding="utf-8") as stream:

        def _append(record: TurnRecord) -> None:
            stream.write(json.dumps(record.to_dict(), ensure_ascii=False))
            stream.write("\n")
            stream.flush()

        records, summaries = await _run_matrix(
            args,
            tokenizer=tokenizer,
            providers=providers,
            sizes=sizes,
            strategies=strategies,
            on_record=_append,
        )

    if not summaries:
        print("No benchmark cells ran; check the provider configuration.", flush=True)
        return 1

    cache_read_ratio = None if args.no_cost else args.cache_read_ratio
    print()
    print(render_summary_table(summaries, cache_read_ratio=cache_read_ratio))

    summary_path = args.out / f"{args.run_id}-summary.csv"
    write_summary_csv(summary_path, summaries, cache_read_ratio=cache_read_ratio)
    print()
    print(f"Wrote {records_path}")
    print(f"Wrote {summary_path}")

    if args.dry_run:
        estimated = sum(record.sent_tokens_local for record in records)
        print(
            f"\nDry run. A live run of this matrix would send roughly {estimated:,} prompt tokens."
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the benchmark.

    Args:
        argv: Argument vector. Defaults to ``sys.argv[1:]``.

    Returns:
        A process exit code.
    """
    args = build_parser().parse_args(argv)
    return asyncio.run(run_benchmark(args))
