"""Share request-only metadata and measure options outside conversation messages."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any, Final, cast

from agent_framework import FunctionTool, Message, SessionContext, TokenizerProtocol
from agent_framework import tool as function_tool

from ._summary_links import has_summary_links

RECALL_PROMPT_KEY: Final[str] = "_maf_recall_prompt"


def as_compaction_context(message: Message) -> Message:
    """Attribute derived request context so input-history sinks do not append it."""
    context = SessionContext(input_messages=[])
    context.extend_messages("maf_compaction_context", [message])
    return context.context_messages["maf_compaction_context"][0]


def attribute_compaction_context(
    messages: Sequence[Message], original_contents: Mapping[int, object]
) -> None:
    """Keep derived summaries out of the finalized request's new-input history."""
    from ._toolsummary import _is_written_record  # pyright: ignore[reportPrivateUsage]

    for message in messages:
        if (
            id(message.contents) not in original_contents
            and "_attribution" not in message.additional_properties
            and (has_summary_links(message) or _is_written_record(message))
        ):
            message.additional_properties.update(
                as_compaction_context(message).additional_properties
            )


def request_tools(options: Mapping[str, Any]) -> list[Any]:
    """Normalize the single-tool and sequence forms accepted by chat options."""
    tools = options.get("tools")
    if tools is None:
        return []
    return (
        list(cast(Sequence[Any], tools))
        if isinstance(tools, Sequence) and not isinstance(tools, str)
        else [tools]
    )


def tool_spec(tool: Any) -> dict[str, Any]:
    """Read the current schema, including changes to descriptions and input models."""
    if isinstance(tool, FunctionTool):
        return tool.to_json_schema_spec()
    if isinstance(tool, Mapping):
        return dict(cast(Mapping[str, Any], tool))
    if callable(tool):
        return function_tool(tool).to_json_schema_spec()
    raise TypeError(f"Cannot estimate request overhead for tool type {type(tool).__name__}.")


class RequestOverhead:
    """Cache token counts by serialized schema rather than tool identity."""

    def __init__(self, tokenizer: TokenizerProtocol) -> None:
        self.tokenizer = tokenizer
        self._count = lru_cache(maxsize=256)(tokenizer.count_tokens)

    def count(self, options: Mapping[str, Any]) -> int:
        instructions = options.get("instructions") or ""
        total = self.tokenizer.count_tokens(instructions) if instructions else 0
        for tool in request_tools(options):
            serialized = json.dumps(tool_spec(tool), ensure_ascii=False, sort_keys=True)
            total += self._count(serialized)
        return total
