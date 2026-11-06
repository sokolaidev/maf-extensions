"""Named synthetic host policies for reproducible capability measurements."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_framework import Content, Message


@dataclass(frozen=True)
class FixtureMediaEstimate:
    """A fixed host estimate, independent of fixture encoding and provider billing."""

    tokens: int

    def __call__(self, part: Content) -> int:
        return self.tokens


async def describe_fixture(part: Content, turn: Message) -> str | None:
    """Describe fixture media without contacting a model or fetching its payload."""
    return f"Fixture {part.media_type}: CODE-2466."


async def decline_fixture(part: Content, turn: Message) -> None:
    """Decline the description so compaction stops at this turn."""
    return None


def is_pdf(part: Content) -> bool:
    """Select PDF fixtures and preserve other media turns."""
    return part.media_type == "application/pdf"


def media_kwargs(profile: str) -> dict[str, Any]:
    """Resolve a serializable profile into user-summary constructor hooks."""
    if profile in {"none", "preserve"}:
        return {}
    if profile == "decline":
        return {"describe_media": decline_fixture}
    return {
        "describe_media": describe_fixture,
        **({"describes_media": is_pdf} if profile == "select_pdf" else {}),
    }


def rewrite_fixture(content: Content, text: str) -> bool:
    """Shorten a single text item while retaining its metadata and all other items."""
    texts = [item for item in content.items or [] if item.type == "text"]
    if len(texts) != 1 or texts[0].raw_representation is not None:
        return False
    texts[0].text = text
    return True


def invalid_rewrite(content: Content, text: str) -> bool:
    """Deliberately remove media to exercise compaction's rewrite validation."""
    content.items = [Content.from_text(text)]
    return True
