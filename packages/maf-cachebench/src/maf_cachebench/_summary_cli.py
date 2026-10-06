"""Command line entry point for the combined cost-and-correctness summary."""

from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import Sequence

from agent_framework import Message, apply_compaction

from ._advisor import ModelPricing, fetch_openrouter_pricing
from ._cli_selection import (
    require_baseline,
    select_standalone_strategies,
    standalone_strategy_names,
    validate_pricing_options,
    validate_recall_counts,
)
from ._metrics import clamp_cached_tokens, serialize_message
from ._providers import build_provider, parse_provider_selector, provider_names
from ._recall import RecallScore, build_recall_scenario, score_answer
from ._runner import CallOutcome, ProviderCaller
from ._strategies import StrategyOptions, build_strategy
from ._summary import (
    DEFAULT_MIN_CORRECTNESS,
    JointOutcome,
    JointVerdict,
    recommend,
    relative_correctness,
)
from ._tokenizers import TOKENIZER_NAMES, build_tokenizer

__all__ = ["build_parser", "main", "run_summary"]

_DEFAULT_STRATEGIES = "none,truncation,context_window,context_window_aggressive"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        A parser for the combined summary.
    """
    parser = argparse.ArgumentParser(
        prog="cachebench-summary",
        description=(
            "Judge compaction on cost and correctness together, measured on one conversation, "
            "and recommend the cheapest strategy that still answers correctly."
        ),
    )
    parser.add_argument("provider", help="Provider or provider:model.")
    parser.add_argument(
        "--strategies",
        default=_DEFAULT_STRATEGIES,
        help=f"Available: {','.join(standalone_strategy_names())}",
    )
    parser.add_argument(
        "--filler-turns", type=int, default=9, help="Padding turns between planted facts."
    )
    parser.add_argument(
        "--filler-tokens", type=int, default=5_000, help="Approximate size of each filler exchange."
    )
    parser.add_argument(
        "--context-window", type=int, default=48_000, help="Simulated context window."
    )
    parser.add_argument(
        "--max-output-tokens", type=int, default=2_048, help="Output reservation for budget math."
    )
    parser.add_argument(
        "--answer-max-tokens", type=int, default=800, help="Cap on the final answer."
    )
    parser.add_argument(
        "--min-correctness",
        type=float,
        default=DEFAULT_MIN_CORRECTNESS,
        help=(
            "Fraction of the control's correctness a strategy must retain to be eligible. Default "
            "0.9."
        ),
    )
    parser.add_argument(
        "--price-input", type=float, default=None, help="Input price per million tokens."
    )
    parser.add_argument(
        "--price-cached", type=float, default=None, help="Cached-read price per million tokens."
    )
    parser.add_argument(
        "--tokenizer", default="estimator", choices=list(TOKENIZER_NAMES), help="Token counter."
    )
    parser.add_argument(
        "--no-temperature", action="store_true", help="Omit temperature for models that reject it."
    )
    parser.add_argument(
        "--request-timeout", type=float, default=300.0, help="Per-call timeout in seconds."
    )
    parser.add_argument(
        "--show-answers", action="store_true", help="Print each final answer in full."
    )
    return parser


def _resolve_pricing(args: argparse.Namespace, provider: str, model: str) -> ModelPricing:
    """Resolve pricing from the command line or OpenRouter's catalogue.

    Raises:
        SystemExit: If prices are neither supplied nor discoverable.
    """
    validate_pricing_options(args)
    if args.price_input is not None:
        return ModelPricing(
            args.price_input,
            args.price_cached if args.price_cached is not None else args.price_input,
        )
    if provider == "openrouter":
        try:
            return fetch_openrouter_pricing(model)
        except (KeyError, OSError, ValueError) as error:
            raise SystemExit(
                f"Could not fetch pricing for {model!r}: {error}. Pass --price-input."
            ) from error
    raise SystemExit(
        f"--price-input is required for provider {provider!r} (only OpenRouter pricing is "
        f"auto-fetched)."
    )


async def _measure(
    args: argparse.Namespace,
    provider: str,
    model_override: str | None,
    strategy_name: str,
    pricing: ModelPricing,
) -> JointOutcome:
    """Replay the scenario under one strategy, sending every turn, and score the answer."""
    tokenizer = build_tokenizer(args.tokenizer)
    salt = f"{time.strftime('%Y%m%d-%H%M%S')}-{strategy_name}"
    scenario = build_recall_scenario(
        salt=salt, filler_turns=args.filler_turns, filler_tokens=args.filler_tokens
    )
    strategy = build_strategy(
        strategy_name,
        StrategyOptions(
            tokenizer=tokenizer,
            max_context_window_tokens=args.context_window,
            max_output_tokens=args.max_output_tokens,
        ),
    )
    runtime = build_provider(
        provider,
        temperature=None if args.no_temperature else 0.0,
        response_max_tokens=args.answer_max_tokens,
        model=model_override,
    )
    # Intermediate turns are billed but their output is discarded, so cap generation there;
    # only the final turn needs room for a real answer.
    interim = ProviderCaller(
        runtime, extra_options={"max_tokens": 16}, request_timeout=args.request_timeout or None
    )
    final = ProviderCaller(runtime, request_timeout=args.request_timeout or None)

    async def measured(caller: ProviderCaller, messages: list[Message]) -> CallOutcome:
        outcome = await caller(messages)
        if outcome.error or not outcome.input_tokens or outcome.input_tokens < 0:
            reason = outcome.error or "provider omitted positive input usage"
            raise SystemExit(f"Cannot measure {strategy_name!r}: {reason}.")
        return outcome

    history: list[Message] = [scenario.transcript.system]
    input_tokens = cached_tokens = 0
    turns = scenario.transcript.turns
    projected: list[Message] = []
    for turn in turns[:-1]:
        history.extend(turn.request)
        projected = await apply_compaction(history, strategy=strategy, tokenizer=tokenizer)
        outcome = await measured(interim, projected)
        input_tokens += outcome.input_tokens or 0
        cached_tokens += clamp_cached_tokens(outcome.input_tokens or 0, outcome.cached_tokens)
        history.extend(turn.reply)

    history.extend(turns[-1].request)
    projected = await apply_compaction(history, strategy=strategy, tokenizer=tokenizer)
    final_prompt = "\n".join(serialize_message(message) for message in projected)
    answer_outcome = await measured(final, projected)
    input_tokens += answer_outcome.input_tokens or 0
    cached_tokens += clamp_cached_tokens(
        answer_outcome.input_tokens or 0, answer_outcome.cached_tokens
    )
    answer = answer_outcome.text or ""

    fresh = max(input_tokens - cached_tokens, 0)
    cost = (
        fresh * pricing.input_per_million + cached_tokens * pricing.cached_read_per_million
    ) / 1_000_000
    return JointOutcome(
        strategy=strategy_name,
        cost=cost,
        input_tokens=input_tokens,
        cached_tokens=cached_tokens,
        messages_left=len(projected),
        messages_total=len(history),
        score=RecallScore(
            outcomes=score_answer(answer, scenario.facts, final_prompt),
            answer=answer,
            messages_left=len(projected),
            messages_total=len(history),
            contradictions=scenario.contradictions,
            error=answer_outcome.error,
        ),
    )


def _render(verdict: JointVerdict, pricing: ModelPricing, model: str, *, show_answers: bool) -> str:
    """Render both axes side by side, then the recommendation."""
    base = verdict.baseline
    header = (
        f"{'strategy':<28}{'msgs_left':>11}{'cost':>10}{'vs none':>9}"
        f"{'hit%':>7}{'correct':>9}{'vs none':>9}{'retracted':>11}"
    )
    lines = [
        "",
        f"Model: {model}",
        (
            f"Pricing: ${pricing.input_per_million:.2f}/M input, "
            f"${pricing.cached_read_per_million:.3f}/M cached ({pricing.cache_discount:.0%} off)"
        ),
        "",
        header,
        "-" * len(header),
    ]
    for outcome in verdict.outcomes:
        cost_delta = (
            "-"
            if outcome.strategy == base.strategy
            else "n/a"
            if base.cost <= 0
            else f"{(outcome.cost - base.cost) / base.cost:+.0%}"
        )
        rel = (
            "-"
            if outcome.strategy == base.strategy
            else f"{relative_correctness(outcome, base):.0%}"
        )
        hit = "n/a" if outcome.hit_rate is None else f"{outcome.hit_rate:.0%}"
        lines.append(
            f"{outcome.strategy:<28}{f'{outcome.messages_left}/{outcome.messages_total}':>11}"
            f"{'$' + format(outcome.cost, '.4f'):>10}{cost_delta:>9}{hit:>7}"
            f"{outcome.correctness:>8.0%}"
            f"{' ' if outcome.strategy != base.strategy else '*'}{rel:>9}"
            f"{('YES' if outcome.score.asserted_superseded else '-'):>11}"
        )
    lines += [
        "",
        (
            "cost      = input charges for the whole conversation, cached tokens priced at their "
            "discount"
        ),
        "correct   = share of correctness checks the final answer passed (* marks the control)",
        "vs none   = change against not compacting, on cost and on correctness respectively",
        "retracted = the answer asserted a decision the user explicitly withdrew",
        "",
        f"VERDICT: {verdict.recommended}",
        verdict.rationale,
    ]
    if show_answers:
        for outcome in verdict.outcomes:
            lines += ["", f"--- {outcome.strategy} ---", outcome.score.answer or "(no answer)"]
    return "\n".join(lines)


async def run_summary(args: argparse.Namespace) -> int:
    """Measure every strategy on both axes and print the combined report.

    Args:
        args: Parsed command line arguments.

    Returns:
        A process exit code.
    """
    strategies = select_standalone_strategies(args.strategies)
    require_baseline(strategies)
    validate_pricing_options(args)
    validate_recall_counts(args)
    provider, model_override = parse_provider_selector(args.provider)
    if provider not in provider_names():
        raise SystemExit(f"Unknown provider {provider!r}. Available: {', '.join(provider_names())}")

    probe = build_provider(
        provider,
        temperature=None if args.no_temperature else 0.0,
        response_max_tokens=16,
        model=model_override,
    )
    pricing = _resolve_pricing(args, provider, probe.model)

    outcomes: list[JointOutcome] = []
    for strategy_name in strategies:
        print(f"-> {strategy_name}", flush=True)
        outcomes.append(await _measure(args, provider, model_override, strategy_name, pricing))

    try:
        verdict = recommend(outcomes, min_correctness=args.min_correctness)
    except ValueError as error:
        raise SystemExit(f"Cannot summarize: {error}") from error
    print(_render(verdict, pricing, probe.model, show_answers=args.show_answers))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and print the combined summary.

    Args:
        argv: Argument vector. Defaults to ``sys.argv[1:]``.

    Returns:
        A process exit code.
    """
    return asyncio.run(run_summary(build_parser().parse_args(argv)))
