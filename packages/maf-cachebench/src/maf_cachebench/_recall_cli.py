"""Command line entry point for the compaction information-loss probe."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from agent_framework import Message, apply_compaction

from ._cli_selection import (
    preflight_strategies,
    select_standalone_strategies,
    standalone_strategy_names,
    validate_generation_caps,
    validate_recall_counts,
    validate_timing_options,
)
from ._metrics import serialize_message
from ._providers import build_provider, parse_provider_selector, provider_names
from ._recall import RecallScore, build_recall_scenario, score_answer
from ._run_identity import new_run_id
from ._runner import ProviderCaller
from ._strategies import StrategyOptions, build_strategy
from ._tokenizers import TOKENIZER_NAMES, build_tokenizer

__all__ = ["build_parser", "main", "run_recall"]

_DEFAULT_STRATEGIES = "none,truncation,context_window,context_window_aggressive"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        A parser for the information-loss probe.
    """
    parser = argparse.ArgumentParser(
        prog="cachebench-recall",
        description=(
            "Measure what each compaction strategy destroys. Plants requirements, a "
            "mid-conversation correction and tool results, then checks whether the model can "
            "still use them in a final answer."
        ),
    )
    parser.add_argument("provider", help="Provider or provider:model.")
    parser.add_argument(
        "--strategies",
        default=_DEFAULT_STRATEGIES,
        help=f"Available: {','.join(standalone_strategy_names())}",
    )
    parser.add_argument("--repeats", type=int, default=1, help="Replays per strategy. Default 1.")
    parser.add_argument(
        "--filler-turns", type=int, default=6, help="Padding turns between planted facts."
    )
    parser.add_argument(
        "--filler-tokens", type=int, default=4_000, help="Approximate size of each filler exchange."
    )
    parser.add_argument(
        "--context-window", type=int, default=32_000, help="Simulated context window."
    )
    parser.add_argument(
        "--max-output-tokens", type=int, default=2_048, help="Output reservation for budget math."
    )
    parser.add_argument(
        "--answer-max-tokens", type=int, default=800, help="Cap on the final answer. Default 800."
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


async def _probe(
    args: argparse.Namespace,
    provider: str,
    model_override: str | None,
    strategy_name: str,
    repeat: int,
):
    """Replay the scenario under one strategy and score the final answer."""
    tokenizer = build_tokenizer(args.tokenizer)
    salt = f"{new_run_id()}-{strategy_name}-{repeat}"
    scenario = build_recall_scenario(
        salt=salt,
        filler_turns=args.filler_turns,
        filler_tokens=args.filler_tokens,
    )
    options = StrategyOptions(
        tokenizer=tokenizer,
        max_context_window_tokens=args.context_window,
        max_output_tokens=args.max_output_tokens,
    )
    strategy = build_strategy(strategy_name, options)

    runtime = build_provider(
        provider,
        temperature=None if args.no_temperature else 0.0,
        response_max_tokens=args.answer_max_tokens,
        model=model_override,
    )
    caller = ProviderCaller(runtime, request_timeout=args.request_timeout or None)

    history: list[Message] = [scenario.transcript.system]
    turns = scenario.transcript.turns
    for turn in turns[:-1]:
        history.extend(turn.request)
        # Compaction runs on every turn, exactly as it would in a real agent loop, so the
        # final prompt reflects the cumulative damage rather than a single trim.
        await apply_compaction(history, strategy=strategy, tokenizer=tokenizer)
        history.extend(turn.reply)

    history.extend(turns[-1].request)
    projected = await apply_compaction(history, strategy=strategy, tokenizer=tokenizer)
    final_prompt = "\n".join(serialize_message(message) for message in projected)

    outcome = await caller(projected)
    answer = outcome.text or ""
    return RecallScore(
        outcomes=score_answer(answer, scenario.facts, final_prompt),
        answer=answer,
        messages_left=len(projected),
        messages_total=len(history),
        contradictions=scenario.contradictions,
        error=outcome.error,
    )


def _render(rows: list[tuple[str, int, RecallScore]], *, show_answers: bool) -> str:
    """Render the probe results."""
    header = (
        f"{'strategy':<28}{'msgs_left':>11}{'facts_left':>12}{'recall':>8}{'lost':>6}{'ignored':>9}"
        f"  {'req':>5} {'corr':>5} {'tool':>5}{'correct':>9}{'retracted':>11}"
    )
    lines = ["", header, "-" * len(header)]
    for strategy, _repeat, score in rows:
        if score.error:
            lines.append(f"{strategy:<28}{'':>6}{'':>6}{'ERROR':>8}  {score.error[:40]}")
            continue

        def _kind(kind: str, score: RecallScore = score) -> str:
            group = score.by_kind(kind)
            return f"{sum(1 for entry in group if entry.recalled)}/{len(group)}" if group else "-"

        left = f"{score.messages_left}/{score.messages_total}"
        facts = f"{score.facts_left}/{len(score.outcomes)}"
        lines.append(
            f"{strategy:<28}{left:>11}{facts:>12}"
            f"{score.recall_rate:>7.0%}{score.lost_to_compaction:>6}{score.ignored_by_model:>9}  "
            f"{_kind('requirement'):>5} {_kind('correction'):>5} {_kind('tool_result'):>5}"
            f"{score.correctness_score:>8.0%}{'*' if score.is_correct else ' '}"
            f"{('YES' if score.asserted_superseded else '-'):>11}"
        )
    lines += [
        "",
        "msgs_left  = messages surviving compaction, out of the uncompacted conversation",
        "facts_left = planted facts surviving compaction: the ceiling on what recall can reach",
        "recall     = facts the model actually used in its answer",
        "lost       = compaction removed it, so the model could not use it  <- the damage",
        "ignored    = still in context but unused: the model's failing, not compaction's",
        "req/corr/tool = recalled requirements, correction, and tool results",
        "correct    = share of checks passed: one per planted fact, one per retraction avoided",
        "            (* marks a perfect score; recall cannot fall when an answer is confidently",
        "            wrong)",
        (
            "retracted  = the answer states the superseded decision — confidently wrong, not "
            "merely incomplete"
        ),
    ]
    if show_answers:
        for strategy, repeat, score in rows:
            lines += ["", f"--- {strategy} #{repeat} ---", score.answer or "(no answer)"]
    return "\n".join(lines)


async def run_recall(args: argparse.Namespace) -> int:
    """Run the probe for every selected strategy and print the comparison.

    Args:
        args: Parsed command line arguments.

    Returns:
        A process exit code.
    """
    strategies = select_standalone_strategies(args.strategies)
    validate_recall_counts(args)
    validate_generation_caps(args)
    validate_timing_options(args)
    provider, model_override = parse_provider_selector(args.provider)
    if provider not in provider_names():
        raise SystemExit(f"Unknown provider {provider!r}. Available: {', '.join(provider_names())}")

    if args.repeats <= 0:
        raise SystemExit("--repeats must be greater than 0.")

    preflight_strategies(
        strategies,
        StrategyOptions(
            tokenizer=build_tokenizer(args.tokenizer),
            max_context_window_tokens=args.context_window,
            max_output_tokens=args.max_output_tokens,
        ),
    )

    rows: list[tuple[str, int, RecallScore]] = []
    for strategy_name in strategies:
        for repeat in range(1, args.repeats + 1):
            print(f"-> {strategy_name} #{repeat}", flush=True)
            rows.append(
                (
                    strategy_name,
                    repeat,
                    await _probe(args, provider, model_override, strategy_name, repeat),
                )
            )
    print(_render(rows, show_answers=args.show_answers))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the probe.

    Args:
        argv: Argument vector. Defaults to ``sys.argv[1:]``.

    Returns:
        A process exit code.
    """
    return asyncio.run(run_recall(build_parser().parse_args(argv)))
