"""Strategy selections for commands without a summarizer or live-agent middleware."""

from ._strategies import forces_records, needs_summarizer, strategy_names


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
    known = standalone_strategy_names()
    unsupported = [name for name in selected if name not in known]
    if unsupported:
        raise SystemExit(
            f"Unsupported strategies: {', '.join(unsupported)}. Available: {','.join(known)}. "
            "Use cachebench_live for strategies requiring a summarizer or recall middleware."
        )
    return selected
