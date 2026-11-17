"""Place a lookup across the sending-budget boundary in a live qualification."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, override

from agent_framework._compaction import annotate_token_counts, included_token_count
from maf_compaction import ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy

from ._transcripts import TRUE_CHARS_PER_TOKEN, sized_text

if TYPE_CHECKING:
    from agent_framework import Message

PRESSURE_REQUEST = "Merge qualification checkpoint. Call the required lookup now."
_PADDING_HEADER = "\n\nSynthetic padding without facts. Ignore these words in the record. "
_HEADROOM = 24


class MergeQualificationStrategy(ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy):
    """Size the checkpoint input using the real view immediately before compaction."""

    @override
    async def compact_before(self, messages: list[Message], *, options: Mapping[str, Any]) -> bool:
        if not messages:
            return await super().compact_before(messages, options=options)
        request = messages[-1]
        if (
            request.role != "user"
            or len(request.contents) != 1
            or request.contents[0].type != "text"
            or not (text := request.contents[0].text)
            or PRESSURE_REQUEST not in text
            or _PADDING_HEADER in text
        ):
            return await super().compact_before(messages, options=options)

        budget = self.sending_budget(options)
        self._pad_checkpoint(messages, text, budget)
        changed = await super().compact_before(messages, options=options)
        if messages and messages[-1] is request:
            # Ordinary reductions can leave room after the checkpoint was sized.
            self._pad_checkpoint(messages, request.text, budget)
        return changed

    def _pad_checkpoint(self, messages: list[Message], text: str, budget: int) -> None:
        annotate_token_counts(messages, tokenizer=self.tokenizer)
        available = budget - included_token_count(messages) - _HEADROOM
        if available <= 0:
            return
        content = messages[-1].contents[0]
        index = len(messages) - 1
        padding = sized_text(_PADDING_HEADER, 2799, int(available * 0.85), TRUE_CHARS_PER_TOKEN)
        base = text + padding
        content.text = base
        annotate_token_counts(messages, tokenizer=self.tokenizer, from_index=index)
        remaining = budget - included_token_count(messages) - _HEADROOM
        if remaining < 0:
            base = text + _PADDING_HEADER
            content.text = base
            annotate_token_counts(messages, tokenizer=self.tokenizer, from_index=index)
            remaining = budget - included_token_count(messages) - _HEADROOM
        rate = max(self.tokenizer.count_tokens(" audit" * 128), 1) / 128
        content.text = base + " audit" * max(int((remaining - 4) / rate), 0)
        annotate_token_counts(messages, tokenizer=self.tokenizer, from_index=index)
        if included_token_count(messages) > budget:
            content.text = text
            annotate_token_counts(messages, tokenizer=self.tokenizer, from_index=index)
