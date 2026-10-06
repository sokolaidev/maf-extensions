"""Selection validation before benchmark provider setup."""

import argparse
import math
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from agent_framework import TokenizerProtocol

from ._providers import parse_provider_selector, provider_names
from ._runner import validate_duration
from ._strategies import (
    StrategyOptions,
    build_strategy,
    forces_records,
    needs_summarizer,
    resolve_context_window,
    strategy_names,
)
from ._transcripts import build_preset

if TYPE_CHECKING:
    from agent_framework._clients import SupportsChatGetResponse


def validate_unique_selection(name: str, selected: Sequence[str]) -> None:
    """Reject repeated cells before they can share a cache namespace."""
    if len(set(selected)) != len(selected):
        raise SystemExit(f"Duplicate {name} selections are not allowed.")


def validate_summarizer_selector(selector: str | None) -> None:
    """Check a supplied summarizer provider before constructing clients or output files."""
    if selector is not None:
        provider, _ = parse_provider_selector(selector)
        if provider not in provider_names():
            raise SystemExit(f"Unknown summarizer provider {provider!r}.")


def replay_strategy_names() -> list[str]:
    """Return strategies that do not require live-agent middleware."""
    return [
        name
        for name in strategy_names()
        if not forces_records([name]) and name != "user_summary_anchored"
    ]


def standalone_strategy_names() -> list[str]:
    """Return strategies the advisor, recall and summary commands can execute."""
    return [
        name
        for name in strategy_names()
        if not needs_summarizer([name]) and not forces_records([name])
    ]


def select_standalone_strategies(value: str) -> list[str]:
    """Validate a non-empty strategy selection before any provider work."""
    selected = [entry.strip() for entry in value.split(",") if entry.strip()]
    if not selected:
        raise SystemExit("At least one strategy is required.")
    validate_unique_selection("strategy", selected)
    known = standalone_strategy_names()
    unsupported = [name for name in selected if name not in known]
    if unsupported:
        raise SystemExit(
            f"Unsupported strategies: {', '.join(unsupported)}. Available: {','.join(known)}. "
            "Use cachebench_live for strategies requiring a summarizer or recall middleware."
        )
    return selected


def validate_pricing_options(args: argparse.Namespace) -> None:
    """Require an input rate before accepting individual price overrides."""
    for name in (
        "price_input",
        "price_cached",
        "price_output",
        "price_cache_write",
        "price_long_input",
        "price_long_cached",
        "price_long_output",
        "price_long_cache_write",
    ):
        value = getattr(args, name, None)
        if value is not None and (not math.isfinite(value) or value < 0):
            raise SystemExit(f"--{name.replace('_', '-')} must be finite and non-negative.")
    threshold = getattr(args, "long_context_threshold", None)
    if threshold is not None and threshold <= 0:
        raise SystemExit("--long-context-threshold must be greater than 0.")
    if args.price_input is None:
        for name in ("price_cached", "price_output", "price_cache_write"):
            if getattr(args, name, None) is not None:
                raise SystemExit(f"--{name.replace('_', '-')} needs --price-input.")


def validate_recall_counts(args: argparse.Namespace) -> None:
    """Keep the recorded workload counts equal to the scenario that executes."""
    for name, minimum in (
        ("tool_turns", 3),
        ("markers_per_tool", 1),
        ("filler_tool_turns", 0),
        ("filler_turns", 0),
        ("filler_tokens", 0),
    ):
        if getattr(args, name, minimum) < minimum:
            raise SystemExit(f"--{name.replace('_', '-')} must be at least {minimum}.")


def require_baseline(strategies: Sequence[str]) -> None:
    """Require both the control and a compaction alternative before measurement."""
    if "none" not in strategies:
        raise SystemExit("The 'none' baseline must be included.")

    if not any(name != "none" for name in strategies):
        raise SystemExit("At least one non-none strategy is required for a comparison.")


class _PreflightSummarizer:
    """Validate constructors without creating a provider client."""

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse provider work during preflight."""
        raise RuntimeError("preflight must not call a summarizer")


def preflight_strategies(strategies: Sequence[str], options: StrategyOptions) -> None:
    """Validate selected constructors without provider work or output changes."""
    for name in strategies:
        built_from = options
        if needs_summarizer([name]) and options.summarizer is None:
            built_from = replace(
                options, summarizer=cast("SupportsChatGetResponse[Any]", _PreflightSummarizer())
            )
        try:
            build_strategy(name, built_from)
        except ValueError as error:
            raise SystemExit(
                f"{name} rejects this configuration: {error} Each parameter named there is the "
                "flag of the same name, with underscores written as dashes."
            ) from error


def preflight_replay(
    args: argparse.Namespace,
    strategies: Sequence[str],
    sizes: Sequence[str],
    tokenizer: TokenizerProtocol,
) -> None:
    """Resolve and validate each transcript budget before creating clients or files."""
    for size in sizes:
        transcript = build_preset(size, salt="preflight", tokenizer=tokenizer)
        preflight_strategies(
            strategies,
            StrategyOptions(
                tokenizer=tokenizer,
                max_context_window_tokens=resolve_context_window(
                    transcript.approx_final_prompt_tokens,
                    override=args.context_window,
                    max_output_tokens=args.max_output_tokens,
                ),
                max_output_tokens=args.max_output_tokens,
                keep_last_groups=getattr(args, "keep_last_groups", 6),
                keep_last_tool_call_groups=getattr(args, "keep_tool_groups", 4),
            ),
        )


def validate_generation_caps(args: argparse.Namespace) -> None:
    """Reject unusable token limits while preserving zero-valued optional record controls."""
    for name in ("answer_max_tokens", "response_max_tokens"):
        value = getattr(args, name, None)
        if value is not None and value <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive.")
    for name in ("record_max_tokens", "record_target_tokens", "max_groups_before_record"):
        value = getattr(args, name, None)
        if value is not None and value < 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be non-negative.")


def validate_timing_options(args: argparse.Namespace) -> None:
    """Reject invalid waits before provider work, keeping zero as the disable value."""
    for name in ("request_timeout", "turn_delay"):
        value = getattr(args, name, None)
        if value is not None:
            try:
                validate_duration(value, f"--{name.replace('_', '-')}")
            except ValueError as error:
                raise SystemExit(str(error)) from error
