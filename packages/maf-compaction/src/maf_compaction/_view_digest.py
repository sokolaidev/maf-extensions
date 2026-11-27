"""Content identities shared by a live view and its saved layout."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from agent_framework import Content


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def json_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


_CANONICAL = json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False)


def layout_text_digest(text: str) -> str:
    """The digest of a serialized layout as stored: of its canonical text itself."""
    return hashlib.sha256(text.encode()).hexdigest()


def streamed_layout_digest(value: Any) -> str:
    """:func:`layout_text_digest` of ``canonical_json(value)``, without building that text.

    A long conversation's earlier-turns layout is several MiB; telling whether it changed
    must not serialize all of it on every pass.
    """
    digest = hashlib.sha256()
    for chunk in _CANONICAL.iterencode(value):
        digest.update(chunk.encode())
    return digest.hexdigest()


def content_digest(items: Sequence[Content]) -> str | None:
    """Return None when a host's contents cannot be represented in a portable layout."""
    try:
        return json_digest([item.to_dict() for item in items])
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        return None
