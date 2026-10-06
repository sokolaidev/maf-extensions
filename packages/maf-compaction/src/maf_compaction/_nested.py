"""Finding one strategy inside a composition of them."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast


def find_nested_strategy[StrategyT](strategy: Any, kind: type[StrategyT]) -> StrategyT | None:
    """Return ``strategy`` or the first part of it that is an instance of ``kind``.

    A composition is not an instance of its parts, so a plain ``isinstance`` leaves a composed
    strategy's record half without the middleware it needs. The walk follows ``strategies``,
    the attribute the framework's composed strategy and this package's use for their parts,
    depth-first and in order, so a composition's parts are searched before its later siblings
    and the match is the one that runs first. It is cycle-guarded. A record strategy's
    ``fallback`` is not followed: it is what the strategy degrades into, not a phase it runs.

    Args:
        strategy: A strategy, a composition, or None.
        kind: The class wanted.

    Returns:
        The first match in run order, or None when there is none.
    """
    seen: set[int] = set()
    pending: list[Any] = [strategy]
    while pending:
        candidate = pending.pop()
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        if isinstance(candidate, kind):
            return candidate
        parts: object = getattr(candidate, "strategies", None)
        if isinstance(parts, Sequence) and not isinstance(parts, (str, bytes)):
            pending.extend(reversed(cast(Sequence[Any], parts)))
    return None
