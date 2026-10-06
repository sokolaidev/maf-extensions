"""Selection validation before benchmark provider setup."""

import argparse
import math
from collections.abc import Sequence

from ._providers import parse_provider_selector, provider_names
from ._strategies import forces_records, needs_summarizer, strategy_names


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
    """Require the control before spending calls on a recommendation."""
    if "none" not in strategies:
        raise SystemExit("The 'none' baseline must be included.")
