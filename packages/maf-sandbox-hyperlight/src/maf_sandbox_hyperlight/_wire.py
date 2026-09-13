"""Bounded JSON messages between the host and its trusted worker."""

from __future__ import annotations

import json
from typing import cast


class HyperlightWorkerError(RuntimeError):
    """The native worker failed; acquire a replacement before executing again."""


class HyperlightOutputLimitExceeded(RuntimeError):
    """Combined UTF-8 stdout/stderr exceeded the configured return limit."""


def decode(raw: bytes) -> dict[str, object]:
    """Refuse malformed framing before interpreting a message."""
    if not raw.endswith(b"\n"):
        raise HyperlightWorkerError("worker closed or exceeded the message limit")
    value: object = json.loads(raw)
    if not isinstance(value, dict):
        raise HyperlightWorkerError("worker message must be a JSON object")
    return cast("dict[str, object]", value)


def encode(value: dict[str, object]) -> bytes:
    """Escape line breaks and non-ASCII text in one newline-terminated message."""
    return (json.dumps(value, ensure_ascii=True) + "\n").encode("ascii")
