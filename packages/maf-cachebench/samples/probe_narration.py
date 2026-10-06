"""Find which scenario configuration a model can be measured under.

The compaction comparison is only as good as its uncompacted control. If identical repeats
of that control disagree about how much the agent remembered, nothing can be ranked against
it -- and the disagreement is invisible in a normal run, because the reported figure comes
from one repeat.

Narration can affect control stability. This probe repeats the uncompacted control
across narration and placement configurations and reports which produce consistent recall.

Usage:
    python probe_narration.py foundry:gpt-5.4-mini
    python probe_narration.py openrouter:z-ai/glm-5.2 --repeats 5 --placements spread,buried

Cost: one uncompacted conversation per configuration per repeat. At the defaults that is
9 conversations; scale --context-window and the sizing flags down to make it cheaper, since
what is being measured is the model's consistency rather than any particular window size.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from collections.abc import Sequence

from maf_cachebench import (
    RecallScore,
    StrategyOptions,
    build_live_scenario,
    build_provider,
    build_tokenizer,
    parse_provider_selector,
    run_live,
    score_samples,
    wants_client_side_history,
)
from maf_cachebench._cli_selection import validate_recall_counts, validate_unique_selection

#: Points of correctness range above which a control cannot be ranked against. Matches the
#: limit the live run itself enforces, so a configuration this probe passes is one that run
#: will accept.
MAX_USABLE_RANGE = 20.0


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        A parser for the calibration probe.
    """
    parser = argparse.ArgumentParser(
        description="Measure which narration and placement settings give a stable control for a model."
    )
    parser.add_argument("provider", help="Provider or provider:model.")
    parser.add_argument(
        "--narrations",
        default="neutral,prompted,suppressed",
        help=(
            "Narration modes to try. 'neutral' leaves the framework's own guidance as the "
            "only driver, which is what a typical caller gets; 'prompted' demands every value "
            "appear in the final report; 'suppressed' forbids restating them, so each value "
            "exists in exactly one place."
        ),
    )
    parser.add_argument(
        "--placements",
        default="spread",
        help=(
            "Fact placements to try. 'spread' gives each code its own labelled line and "
            "measures preservation; 'buried' hides them in prose and measures retrieval too; "
            "'head' puts them all at the front, where a head-truncating strategy keeps them "
            "for free."
        ),
    )
    parser.add_argument(
        "--repeats", type=int, default=3, help="Repeats per configuration. 3 is the minimum useful."
    )
    parser.add_argument("--agent", default="harness", help="Agent kind: harness or plain.")
    parser.add_argument(
        "--context-window", type=int, default=60_000, help="Simulated context window."
    )
    parser.add_argument(
        "--max-output-tokens", type=int, default=2_048, help="The model's output ceiling."
    )
    parser.add_argument(
        "--answer-max-tokens", type=int, default=4_000, help="Cap sent as max_tokens per request."
    )
    parser.add_argument(
        "--markers-per-tool", type=int, default=8, help="Codes each tool result carries."
    )
    parser.add_argument("--tool-turns", type=int, default=6, help="Tool-call groups to plant.")
    parser.add_argument(
        "--filler-turns", type=int, default=6, help="Padding turns between planted facts."
    )
    parser.add_argument(
        "--filler-tokens", type=int, default=4_000, help="Size of each filler turn."
    )
    parser.add_argument(
        "--tool-result-tokens", type=int, default=8_000, help="Size of each tool result."
    )
    parser.add_argument(
        "--no-force-tool-calls",
        action="store_true",
        help="For routes rejecting a pinned tool_choice.",
    )
    parser.add_argument(
        "--no-temperature", action="store_true", help="Omit temperature for models that reject it."
    )
    return parser


async def _measure(
    args: argparse.Namespace, narration: str, placement: str
) -> tuple[list[float], list[int], str]:
    """Run the uncompacted control repeatedly under one configuration.

    Returns:
        Correctness scores, facts recalled, and the first error seen (empty if none).
    """
    provider, model_override = parse_provider_selector(args.provider)
    runtime = build_provider(
        provider,
        temperature=None if args.no_temperature else 0.0,
        response_max_tokens=args.answer_max_tokens,
        model=model_override,
    )
    tokenizer = build_tokenizer("tiktoken")
    options = StrategyOptions(
        tokenizer=tokenizer,
        max_context_window_tokens=args.context_window,
        max_output_tokens=args.max_output_tokens,
    )
    scores: list[float] = []
    recalled: list[int] = []
    error = ""
    for repeat in range(args.repeats):
        scenario = build_live_scenario(
            salt=f"{narration}-{placement}-{repeat}",
            filler_turns=args.filler_turns,
            filler_tokens=args.filler_tokens,
            tool_turns=args.tool_turns,
            markers_per_tool=args.markers_per_tool,
            narration=narration,
        )
        outcome = await run_live(
            runtime,
            strategy_name="none",
            options=options,
            scenario=scenario,
            agent_kind=args.agent,
            tool_result_tokens=args.tool_result_tokens,
            force_tool_calls=not args.no_force_tool_calls,
            narration=narration,
            fact_placement=placement,
        )
        if outcome.error and not error:
            error = outcome.error
        # Every probe repeat is its own reading of one snapshot, so each is a sample here.
        # A configuration whose repeats disagree is unusable whether the disagreement came
        # from a fresh conversation or from asking the same one twice.
        for sample_index, outcomes in enumerate(score_samples(outcome, scenario), start=1):
            score = RecallScore(
                outcomes=outcomes,
                answer=chr(10).join(outcome.sample(sample_index)[1]),
                messages_left=outcome.messages_left,
                messages_total=outcome.messages_peak,
                contradictions=scenario.contradictions,
                error=outcome.error,
            )
            scores.append(score.correctness_score * 100)
            recalled.append(score.recalled)
    return scores, recalled, error


async def run(args: argparse.Namespace) -> int:
    """Measure every configuration and report which are usable.

    Returns:
        A process exit code. Non-zero when no configuration was stable enough to measure
        under, which is a result worth failing a script on.
    """
    validate_recall_counts(args)
    if args.repeats <= 0:
        raise SystemExit("--repeats must be greater than 0.")
    narrations = [item.strip() for item in args.narrations.split(",") if item.strip()]
    placements = [item.strip() for item in args.placements.split(",") if item.strip()]
    validate_unique_selection("narration", narrations)
    validate_unique_selection("placement", placements)
    provider, model_override = parse_provider_selector(args.provider)
    runtime = build_provider(
        provider, temperature=None, response_max_tokens=16, model=model_override
    )
    if wants_client_side_history(runtime.client):
        print("note: this client keeps history server-side by default; run_live forces store=False")
        print("so the client sends the conversation and the measurement describes it.\n")

    if args.repeats < 5:
        # Small samples can reveal instability but cannot reliably rank configurations.
        print(f"warning: a range from {args.repeats} repeats is noisy. Use it to detect")
        print(
            "instability, not to rank one configuration above another. --repeats 5+ to compare.\n"
        )

    print(f"model: {runtime.model}   agent: {args.agent}   repeats: {args.repeats}")
    print(
        f"window: {args.context_window:,}   facts: {args.markers_per_tool} per tool x {args.tool_turns} tools\n"
    )
    print(f"{'narration':>11}{'placement':>11}{'median':>9}{'range':>8}{'recalled':>20}  verdict")
    print("-" * 78)

    usable: list[tuple[str, str, float, float]] = []
    for placement in placements:
        for narration in narrations:
            scores, recalled, error = await _measure(args, narration, placement)
            if not scores and not error:
                error = "No completed probe samples."
            if error:
                print(f"{narration:>11}{placement:>11}{'ERROR':>9}  {error[:40]}")
                continue
            median = statistics.median(scores)
            spread = max(scores) - min(scores)
            counts = "/".join(str(value) for value in recalled)
            verdict = "usable" if spread <= MAX_USABLE_RANGE else "NOT RANKABLE"
            print(
                f"{narration:>11}{placement:>11}{median:>8.0f}%{spread:>7.0f}p{counts:>20}  {verdict}"
            )
            if spread <= MAX_USABLE_RANGE:
                usable.append((narration, placement, median, spread))

    print()
    if not usable:
        print("No configuration held still. Every accuracy ranking taken here would be noise.")
        print("Try more repeats, fewer markers per tool, or a narration mode that states")
        print("explicitly what the model should do with the values it looks up.")
        return 1

    # Highest median first, then tightest spread: a configuration that is stable because the
    # model consistently fails is stable and useless.
    best = sorted(usable, key=lambda item: (-item[2], item[3]))[0]
    print(
        f"Most measurable: --narration {best[0]} --fact-placement {best[1]} "
        f"({best[2]:.0f}% median, {best[3]:.0f}-point range)."
    )
    print()
    print("Stability is not the only consideration. 'neutral' is the configuration a typical")
    print("caller gets, so if it is unstable that instability is itself a finding about the")
    print("model, and suppressing narration to obtain a clean number measures something else:")
    print("preservation alone, with each value existing in exactly one place. Report which one")
    print("you used.")
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
