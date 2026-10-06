"""Selection validation before benchmark provider setup."""

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
