"""Decision counters and measurements of the current history."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

from agent_framework import Message, TokenizerProtocol
from agent_framework._compaction import (
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
    annotate_token_counts,
    included_token_count,
)
from maf_compaction import USER_SUMMARY_MARKER

COUNTER_LABELS = {
    "records_found": "REC",
    "fallbacks_used": "FALLBACK",
    "fallbacks_after_record": "RECFALLBACK",
    "fallbacks_held_after_record": "RECHELD",
    "forced_calls": "FORCED",
    "reforced_calls": "REFORCED",
    "records_forced": "RECFORCED",
    "records_volunteered": "RECVOLUNTEERED",
    "records_truncated": "TRUNCATED",
    "ignored_forced_calls": "RECALLIGNORED",
    "failed_forced_calls": "RECALLFAILED",
    "declined_collapses": "NOGAIN",
    "user_compactions": "USERCOMPACT",
    "user_summaries_replayed": "USERREPLAY",
    "user_passes_below_trigger": "USERUNDER",
    "user_passes_declined": "USERHELD",
    "user_passes_waited": "USERWAIT",
    "user_summary_failures": "USERSUMMFAIL",
    "user_summary_skips": "USERSUMMSKIP",
    "user_folds": "USERFOLD",
    "records_merged": "RECMERGE",
    "record_merges_rejected": "RECMERGEREJ",
    "user_summaries_merged": "USERMERGE",
    "user_merges_rejected": "USERMERGEREJ",
    "record_rewrites": "RECHARDER",
    "record_rewrites_rejected": "RECHARDERREJ",
    "record_rewrites_skipped": "RECHARDERSKIP",
    "record_merges_skipped": "RECMERGESKIP",
    "record_summary_failures": "RECSUMMFAIL",
    "last_resort_fallbacks": "LASTFALLBACK",
    "chain_targets_reached": "CHAINTARGET",
    "chain_targets_missed": "CHAINSHORT",
    "chain_decisions_kept": "CHAINKEPT",
}


OBSERVATION_LABELS = {
    "records_in_conversation": "RECORDS",
    "user_messages_replaced": "USERREPLACED",
    "user_summaries_in_conversation": "USERSUMMARIES",
    "user_summary_tokens": "USERSUMMTOKENS",
}


def counters(strategy: Any, recall: Any) -> dict[str, int]:
    """Read cumulative counters without counting a composed proxy twice."""
    result: dict[str, int] = {}
    for name in COUNTER_LABELS:
        values = [getattr(source, name, 0) for source in (strategy, recall)]
        result[name] = max(value if isinstance(value, int) else 0 for value in values)
    return result


def summary_measurements(messages: list[Message], tokenizer: TokenizerProtocol) -> dict[str, int]:
    """Measure linked summaries still included in history, including decreases after folds."""
    summaries: list[Message] = []
    for message in messages:
        if (
            message.role != "user"
            or not message.text.startswith(USER_SUMMARY_MARKER)
            or message.additional_properties.get(EXCLUDED_KEY)
        ):
            continue
        annotation = message.additional_properties.get(GROUP_ANNOTATION_KEY)
        if not isinstance(annotation, dict):
            continue
        links = cast(dict[str, Any], annotation)
        if all(
            isinstance(ids := links.get(key), list)
            and bool(cast(list[Any], ids))
            and all(isinstance(value, str) and value for value in cast(list[Any], ids))
            for key in (SUMMARY_OF_MESSAGE_IDS_KEY, SUMMARY_OF_GROUP_IDS_KEY)
        ):
            summaries.append(deepcopy(message))
    annotate_token_counts(summaries, tokenizer=tokenizer, force_retokenize=True)
    return {
        "user_summaries_in_conversation": len(summaries),
        "user_summary_tokens": included_token_count(summaries),
    }
