"""Read JSON measurements without accepting quoted model or tool output."""

from __future__ import annotations

import json
import math
import re
from typing import Any


def records(output: str) -> list[dict[str, Any]]:
    """Return host JSON records, rejecting malformed or non-object measurements."""
    found: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = re.fullmatch(r"\s*\[measured\] (\{.*)", line)
        if match:
            value = json.loads(match[1])
            if not isinstance(value, dict):
                raise ValueError("Measurement must be an object")
            found.append(value)
    return found


def positive_integer(value: object) -> bool:
    """Reject booleans and missing or non-positive counters."""
    return type(value) is int and value > 0


def duration(value: object) -> bool:
    """Require a finite non-negative elapsed measurement."""
    return (
        isinstance(value, (float, int))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )
