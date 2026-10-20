"""Share request-only metadata and measure options outside conversation messages."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any, Final, cast

from agent_framework import FunctionTool, TokenizerProtocol
from agent_framework import tool as function_tool

RECALL_PROMPT_KEY: Final[str] = "_maf_recall_prompt"


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
