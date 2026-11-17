"""Compare merge floors with cachebench's deterministic replay and prefix oracle.

No provider is contacted. Budget failures are counted before projecting their retained
history for measurement; these diagnostic projections would be refused by a real host.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_framework import BaseChatClient, CharacterEstimatorTokenizer, ChatResponse, Message
from maf_cachebench import CallOutcome, CellKey, Transcript, TranscriptTurn, run_cell
from maf_compaction import (
    RECALL_TOOL_NAME,
    CompactionBudgetExceeded,
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    UserTurnAnchoredSummarizationCompactionStrategy,
    make_recall_tool,
)

VALUES = re.compile(r"VAL-(\d{3})-(\d{2})")
BUDGET = 1_200


class Summarizer(BaseChatClient[Any]):
    """Return repeatable 75% or 62.5% coverage of each original record's values."""

    def __init__(self) -> None:
        super().__init__()
        self.requests = 0
        self.input_tokens = 0
        self.counter = CharacterEstimatorTokenizer()

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Awaitable[ChatResponse]:
        if stream:
            raise NotImplementedError("The offline summarizer accepts non-streaming requests only.")
        return self._respond(messages)

    async def _respond(self, messages: Sequence[Message]) -> ChatResponse:
        self.requests += 1
        self.input_tokens += sum(
            self.counter.count_tokens(message.text or "") for message in messages
        )
        values = sorted(set(VALUES.findall(messages[-1].text or "")))
        text = " ".join(
            f"VAL-{record}-{part}"
            for record, part in values
            if int(part) < (6 if int(record) < 4 else 5)
        )
        return ChatResponse(messages=[Message(role="assistant", contents=[text])])


class ObservedStrategy:
    """Count local budget refusals while permitting dry-run measurement of their projection."""

    def __init__(
        self, strategy: ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
    ) -> None:
        self.strategy = strategy
        self.over_budget = 0

    async def __call__(self, messages: list[Message]) -> bool:
        try:
            return await self.strategy(messages)
        except CompactionBudgetExceeded:
            self.over_budget += 1
            return True


def transcript(turns: int) -> Transcript:
    """Seed eight identified recall pairs, then replay short follow-up turns."""
    records: list[Message] = []
    for record in range(8):
        text = " ".join(f"VAL-{record:03}-{part:02}" for part in range(8))
        text += " The completed lookup supplied these details for the comparison." * 24
        call_id = f"record-{record}"
        records.extend(
            [
                Message(
                    role="assistant",
                    contents=[
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": RECALL_TOOL_NAME,
                            "arguments": json.dumps({"values": text}),
                        }
                    ],
                    message_id=f"{call_id}-call",
                ),
                Message(
                    role="tool",
                    contents=[
                        {
                            "type": "function_result",
                            "call_id": call_id,
                            "result": make_recall_tool()(text),
                        }
                    ],
                    message_id=f"{call_id}-result",
                ),
            ]
        )
    return Transcript(
        name="record-heavy-coverage",
        system=Message(
            role="system", contents=["Keep the recorded identifiers."], message_id="system"
        ),
        turns=tuple(
            TranscriptTurn(
                request=tuple(records if turn == 0 else ())
                + (
                    Message(
                        role="user",
                        contents=["Compare the recorded facts."],
                        message_id=f"u-{turn}",
                    ),
                ),
                reply=(
                    Message(
                        role="assistant", contents=["Comparison noted."], message_id=f"a-{turn}"
                    ),
                ),
            )
            for turn in range(turns)
        ),
        approx_final_prompt_tokens=0,
    )


@dataclass(frozen=True)
class Result:
    """One floor's budget, token and independent value-retention measurements."""

    floor: float
    turns: int
    over_budget: int
    projected_tokens: int
    reusable_tokens: int
    summary_requests: int
    summary_input_tokens: int
    omitted_values: int
    final_missing_values: int
    accepted: dict[float, int]


async def compare(floor: float, *, turns: int = 6) -> Result:
    """Replay one fresh cell with a controlled summarizer and no provider traffic."""
    tokenizer = CharacterEstimatorTokenizer()
    summarizer = Summarizer()
    strategy = ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=tokenizer,
        tool_results=ToolResultAnchoredSummarizationCompactionStrategy(
            tokenizer=tokenizer,
            max_input_tokens=BUDGET,
            keep_head_groups=1,
            keep_tail_groups=1,
        ),
        user_turns=UserTurnAnchoredSummarizationCompactionStrategy(
            tokenizer=tokenizer,
            max_input_tokens=BUDGET,
            client=summarizer,
            max_summary_input_tokens=20_000,
            summary_mode="boundary",
            remembered_requests=2,
        ),
        harder_attempts=0,
        chain_seconds=None,
        overflow_policy="raise",
        merge_coverage_floor=floor,
        merge_coverage_step=0.05,
        merge_prompt="Merge the numbered records, copying every identifier.",
    )
    observed = ObservedStrategy(strategy)
    missing = 0

    async def caller(messages: Sequence[Message]) -> CallOutcome:
        nonlocal missing
        values = set(VALUES.findall(json.dumps([message.to_dict() for message in messages])))
        missing = 64 - len(values)
        return CallOutcome(latency_ms=0)

    records = await run_cell(
        cell=CellKey(
            "offline", "controlled-coverage", "record-heavy-coverage", f"floor-{floor}", 0
        ),
        transcript=transcript(turns),
        strategy=observed,
        tokenizer=tokenizer,
        caller=caller,
    )
    return Result(
        floor,
        turns,
        observed.over_budget,
        sum(record.sent_tokens_local for record in records),
        sum(record.reusable_prefix_tokens_local for record in records),
        summarizer.requests,
        summarizer.input_tokens,
        strategy.record_values_omitted,
        missing,
        strategy.record_consolidations_by_coverage,
    )


def report(results: list[Result]) -> str:
    """Render the bounded evidence and its limitations as Markdown."""
    lines = [
        "# Record-merge coverage comparison",
        "",
        "Deterministic offline cachebench replay; no provider requests or provider cache-hit measurements. Each cell replays eight records with 64 distinctive values over six turns, at a 1,200-token budget and 0.5 record share. The controlled summarizer retains six of eight values from the first four records and five of eight from the rest. Splitting stays at depth two. The starting bar is 0.8 and the step is 0.05.",
        "",
        "Budget failures are counted before cachebench measures the retained projection. Projected tokens include these refused passes for comparison; a real host would not send them. Token and reusable-prefix counts use the character estimator, not provider billing. Missing values are audited independently in the final projection; omitted values count accepted consolidation losses, excluding replay.",
        "",
        "| Floor | Over-budget passes | Projected tokens | Reusable prefix tokens | Summary requests | Summary input tokens | Values omitted | Final missing / 64 | Accepted by bar |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        accepted = (
            ", ".join(f"{bar:g}: {count}" for bar, count in sorted(result.accepted.items()))
            or "none"
        )
        lines.append(
            f"| {result.floor:g} | {result.over_budget}/{result.turns} | {result.projected_tokens} "
            f"| {result.reusable_tokens} | {result.summary_requests} | {result.summary_input_tokens} "
            f"| {result.omitted_values} | {result.final_missing_values} | {accepted} |"
        )
    lines.extend(
        [
            "",
            "Run from the repository root: `uv run python scripts/experiments/compare_merge_coverage.py`. These results demonstrate the policy's token/loss tradeoff on controlled answers; they do not establish semantic retention or live model quality.",
            "",
        ]
    )
    return "\n".join(lines)


async def main() -> None:
    """Run four floors once each and print or save the comparison."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    results = [await compare(floor) for floor in (0.8, 0.75, 0.7, 0.6)]
    rendered = report(results)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    asyncio.run(main())
