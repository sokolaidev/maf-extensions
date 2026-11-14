"""Applied record edits must not rebuild a full-history candidate for every edit."""

from __future__ import annotations

import copy
import json
from collections import Counter

import pytest
from agent_framework import Message
from agent_framework._compaction import EXCLUDED_KEY, annotate_message_groups, set_excluded
from test_pass_cost import _applied, _counting, _state
from test_record_budget import _sent, _size, _strategy, _Summarizer, _tool_group

from maf_compaction._composed import ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
from maf_compaction._record_replay import replacement_identity

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("records", [12, 30])
@pytest.mark.parametrize("reload", [False, True])
async def test_applied_edits_do_not_rebuild_candidates_as_history_grows(
    monkeypatch: pytest.MonkeyPatch, records: int, reload: bool
) -> None:
    strategy, summarizer, messages, _ = await _applied(records=records, narration=20)
    asked = len(summarizer.requests)
    calls: Counter[str] = Counter()
    composed = ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
    for name in ("_record_replay_view", "_match_record_edit"):
        _counting(monkeypatch, composed, name, calls)

    for turn in range(3):
        if reload:
            messages = [Message.from_dict(message.to_dict()) for message in messages]
        messages.extend(_tool_group(turn))
        _size(messages)
        before = _state(strategy, messages)

        assert not strategy._replay_record_edits(messages)
        assert _state(strategy, messages) == before

    assert len(summarizer.requests) == asked
    assert calls["_record_replay_view"] == 0
    assert calls["_match_record_edit"] == 0


@pytest.mark.parametrize("records", [12, 30])
async def test_held_record_sources_are_indexed_once_per_replay(
    monkeypatch: pytest.MonkeyPatch, records: int
) -> None:
    from maf_compaction import _composed as composed_module

    strategy, _, messages, _ = await _applied(records=records, narration=20)
    calls: Counter[str] = Counter()
    _counting(monkeypatch, composed_module, "reshapeable_record_groups", calls)
    _counting(monkeypatch, composed_module, "_call_identity", calls)

    assert not strategy._replay_record_edits(messages)

    assert calls["reshapeable_record_groups"] <= 1
    assert calls["_call_identity"] <= records


@pytest.mark.parametrize("change", ["one-source", "all-sources", "lost-outputs", "rebuilt"])
async def test_applied_replay_rechecks_sources_and_outputs_on_the_next_history(change: str) -> None:
    strategy, summarizer, messages, handed = await _applied(records=12, narration=8)
    expected = _sent(messages)
    asked = len(summarizer.requests)
    assert not strategy._replay_record_edits(messages)
    if change == "rebuilt":
        messages = copy.deepcopy(handed)
    elif change == "lost-outputs":
        messages[:] = [message for message in messages if not replacement_identity(message)]
    else:
        for message in messages:
            if message.message_id == "rec0_call" or (
                change == "all-sources" and (message.message_id or "").startswith("rec")
            ):
                set_excluded(message, excluded=False)
    _size(messages)

    assert strategy._replay_record_edits(messages)
    strategy._keep_shedding(messages)

    assert _sent(messages) == expected
    assert len(summarizer.requests) == asked
    assert not strategy._replay_record_edits(messages)


async def test_restored_decisions_replay_after_a_noop_with_different_decisions() -> None:
    strategy, _, messages, handed = await _applied(records=12, narration=8)
    cold = _strategy(_Summarizer(), budget=1_000_000, cap=None)
    _size(handed)
    assert not cold._replay_record_edits(handed)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))

    assert cold._replay_record_edits(handed)
    cold._keep_shedding(handed)

    assert _sent(handed) == _sent(messages)


async def test_replay_without_member_certificates_keeps_the_full_matching_path() -> None:
    strategy, _, messages, handed = await _applied(records=12, narration=8)
    for edit in strategy._record_order:
        edit.members.clear()
    assert not strategy._replay_record_edits(messages)
    annotate_message_groups(handed)

    assert strategy._replay_record_edits(handed)
    strategy._keep_shedding(handed)

    assert _sent(handed) == _sent(messages)
    assert any(message.additional_properties.get(EXCLUDED_KEY) for message in handed)
