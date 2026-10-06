"""Command line entry point for the per-model strategy recommendation."""

from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ._advisor import ModelPricing, Verdict, advise, fetch_openrouter_pricing
from ._cli_selection import select_standalone_strategies, standalone_strategy_names
from ._metrics import summarize_cell
from ._providers import (
    ProviderRuntime,
    build_provider,
    parse_provider_selector,
    prompt_cache_key_options,
    provider_names,
)
from ._runner import ProviderCaller, run_cell
from ._strategies import StrategyOptions, build_strategy, resolve_context_window
from ._tokenizers import TOKENIZER_NAMES, build_tokenizer
from ._transcripts import TRANSCRIPT_PRESETS, build_preset
from ._types import CellKey, CellSummary

__all__ = ["build_parser", "main", "run_advice"]

_DEFAULT_STRATEGIES = "none,truncation,context_window,context_window_aggressive"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        A parser for a single-model strategy recommendation.
    """
    parser = argparse.ArgumentParser(
        prog="cachebench-advise",
        description=(
            "Measure one model against several compaction strategies and recommend the "
            "cheapest, or report that the model is too erratic to rank them."
        ),
    )
    parser.add_argument(
        "provider", help="Provider or provider:model, e.g. openrouter:openai/gpt-5.6-luna"
    )
    parser.add_argument(
        "--strategies",
        default=_DEFAULT_STRATEGIES,
        help=f"Available: {','.join(standalone_strategy_names())}",
    )
    parser.add_argument(
        "--size", default="mid", choices=list(TRANSCRIPT_PRESETS), help="Transcript preset."
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=3,
        help=(
            "Replays per strategy. Two or more are needed to tell a real gap from provider noise. "
            "Default 3."
        ),
    )
    parser.add_argument(
        "--price-input",
        type=float,
        default=None,
        help="Input price per million tokens. Auto-fetched for OpenRouter models when omitted.",
    )
    parser.add_argument(
        "--price-cached",
        type=float,
        default=None,
        help=(
            "Cached-read price per million tokens. Auto-fetched for OpenRouter models when omitted."
        ),
    )
    parser.add_argument(
        "--context-window", type=int, default=None, help="Simulated context window."
    )
    parser.add_argument(
        "--max-output-tokens", type=int, default=512, help="Output reservation for budget math."
    )
    parser.add_argument(
        "--tokenizer", default="estimator", choices=list(TOKENIZER_NAMES), help="Token counter."
    )
    parser.add_argument(
        "--response-max-tokens", type=int, default=16, help="Cap on generated tokens."
    )
    parser.add_argument(
        "--no-temperature", action="store_true", help="Omit temperature for models that reject it."
    )
    parser.add_argument(
        "--request-timeout", type=float, default=300.0, help="Per-call timeout in seconds."
    )
    parser.add_argument("--turn-delay", type=float, default=0.0, help="Seconds between turns.")
    parser.add_argument(
        "--out", type=Path, default=None, help="Optional directory for the per-turn JSONL."
    )
    return parser


def _resolve_pricing(args: argparse.Namespace, provider: str, model: str) -> ModelPricing:
    """Resolve pricing from the command line, or from OpenRouter's catalogue.

    Raises:
        SystemExit: If prices are neither supplied nor discoverable.
    """
    if args.price_input is not None:
        return ModelPricing(
            args.price_input,
            args.price_cached if args.price_cached is not None else args.price_input,
        )
    if provider == "openrouter":
        try:
            return fetch_openrouter_pricing(model)
        except (KeyError, OSError) as error:
            raise SystemExit(
                f"Could not fetch pricing for {model!r}: {error}. Pass --price-input."
            ) from error
    raise SystemExit(
        f"--price-input is required for provider {provider!r} (only OpenRouter pricing is "
        f"auto-fetched)."
    )


async def _measure(
    args: argparse.Namespace, provider: str, runtime: ProviderRuntime
) -> list[CellSummary]:
    """Replay every selected strategy against the model and summarize each cell."""
    tokenizer = build_tokenizer(args.tokenizer)
    run_id = time.strftime("%Y%m%d-%H%M%S")
    summaries: list[CellSummary] = []
    stream: Any = None
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        stream = (args.out / f"advise-{run_id}-records.jsonl").open("w", encoding="utf-8")

    try:
        for strategy_name in [
            entry.strip() for entry in args.strategies.split(",") if entry.strip()
        ]:
            for repeat in range(1, args.repeats + 1):
                cell = CellKey(
                    provider=provider,
                    model=runtime.model,
                    transcript=args.size,
                    strategy=strategy_name,
                    repeat=repeat,
                )
                salt = f"{run_id}-{cell.label}"
                transcript = build_preset(args.size, salt=salt, tokenizer=tokenizer)
                options = StrategyOptions(
                    tokenizer=tokenizer,
                    max_context_window_tokens=resolve_context_window(
                        transcript.approx_final_prompt_tokens,
                        override=args.context_window,
                        max_output_tokens=args.max_output_tokens,
                    ),
                    max_output_tokens=args.max_output_tokens,
                )
                print(f"-> {cell.label}", flush=True)
                records = await run_cell(
                    cell=cell,
                    transcript=transcript,
                    strategy=build_strategy(strategy_name, options),
                    tokenizer=tokenizer,
                    caller=ProviderCaller(
                        runtime,
                        extra_options=prompt_cache_key_options(provider, salt),
                        request_timeout=args.request_timeout or None,
                    ),
                    on_record=(lambda record: _write(stream, record)) if stream else None,
                    turn_delay=args.turn_delay,
                )
                summaries.append(
                    summarize_cell(
                        records,
                        cell=cell,
                        reports_cache_tokens=any(
                            record.cached_tokens is not None for record in records
                        ),
                    )
                )
    finally:
        if stream is not None:
            stream.close()
    return summaries


def _write(stream: Any, record: Any) -> None:
    """Append one record to the open JSONL stream."""
    import json

    stream.write(json.dumps(record.to_dict(), ensure_ascii=False))
    stream.write("\n")
    stream.flush()


def _render(verdict: Verdict, pricing: ModelPricing, model: str) -> str:
    """Render the verdict as the report a human reads."""
    lines = [
        "",
        f"Model: {model}",
        (
            f"Pricing: ${pricing.input_per_million:.2f}/M input, "
            f"${pricing.cached_read_per_million:.3f}/M cached ({pricing.cache_discount:.0%} off)"
        ),
        "",
        f"{'strategy':<28}{'cost/conversation':>19}{'spread':>9}{'cache hit':>11}",
        f"{'-' * 28}{'-' * 19:>19}{'-' * 9:>9}{'-' * 11:>11}",
    ]
    for entry in verdict.ranked:
        hit = "n/a" if entry.hit_rate is None else f"{entry.hit_rate:.0%}"
        marker = "  <- baseline" if entry.strategy == verdict.baseline.strategy else ""
        lines.append(
            f"{entry.strategy:<28}{'$' + format(entry.median, '.4f'):>19}"
            f"{entry.spread:>8.0%}{hit:>11}{marker}"
        )

    saving = verdict.saving_fraction
    lines += ["", f"VERDICT: {verdict.recommended}", f"Confidence: {verdict.confidence}", ""]
    if verdict.confidence == "inconclusive":
        lines.append("Cost cannot separate these strategies on this model.")
    elif verdict.recommended == verdict.baseline.strategy:
        lines.append(
            "Compaction does not pay for itself here — keep it off except as an overflow guard."
        )
    else:
        lines.append(
            f"Compacting with {verdict.recommended!r} saves {saving:.0%} against not compacting."
        )
    lines += [
        verdict.rationale,
        "",
        "Cost only. Compaction's real purpose is preventing context overflow, and this says",
        "nothing about whether the agent still behaves correctly once history is discarded.",
    ]
    return "\n".join(lines)


async def run_advice(args: argparse.Namespace) -> int:
    """Measure the model and print a recommendation.

    Args:
        args: Parsed command line arguments.

    Returns:
        A process exit code.
    """
    select_standalone_strategies(args.strategies)
    provider, model_override = parse_provider_selector(args.provider)
    if provider not in provider_names():
        raise SystemExit(f"Unknown provider {provider!r}. Available: {', '.join(provider_names())}")
    if args.repeats <= 0:
        raise SystemExit("--repeats must be greater than 0.")

    pricing = None
    if provider != "openrouter" or args.price_input is not None:
        pricing = _resolve_pricing(args, provider, model_override or provider)
    runtime = build_provider(
        provider,
        temperature=None if args.no_temperature else 0.0,
        response_max_tokens=args.response_max_tokens,
        model=model_override,
    )
    model = runtime.model
    if pricing is None:
        pricing = _resolve_pricing(args, provider, model)
    summaries = await _measure(args, provider, runtime)
    try:
        verdict = advise(summaries, pricing)
    except ValueError as error:
        raise SystemExit(f"Cannot advise: {error}") from error
    print(_render(verdict, pricing, model))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and produce the recommendation.

    Args:
        argv: Argument vector. Defaults to ``sys.argv[1:]``.

    Returns:
        A process exit code.
    """
    return asyncio.run(run_advice(build_parser().parse_args(argv)))
