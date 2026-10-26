"""Cumulative decisions independent of restored or rebuilt behavioral state."""

from __future__ import annotations

from typing import Any

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


OBSERVATION_LABELS = {"user_messages_replaced": "USERREPLACED"}


def counters(strategy: Any, recall: Any) -> dict[str, int]:
    """Read cumulative counters without counting a composed proxy twice."""
    result: dict[str, int] = {}
    for name in COUNTER_LABELS:
        values = [getattr(source, name, 0) for source in (strategy, recall)]
        result[name] = max(value if isinstance(value, int) else 0 for value in values)
    return result
