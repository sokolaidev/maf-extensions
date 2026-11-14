"""What a pass costs once its decisions are applied, and what the cheaper paths rest on.

A pass over a history that already carries every decision changes nothing, and it runs before
every model call, so its cost is the cost of a long conversation. These tests bound that work
in counts rather than seconds -- token recounts while shedding, replay traversals and message
fingerprints per pass -- and pin the properties the bounded forms depend on: a running total
that matches a recount, a replay that is a fixpoint, and a fingerprint cache that never
answers for content it did not read.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from typing import Any

import pytest
from agent_framework import Content, Message
from agent_framework._compaction import (
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    annotate_message_groups,
    annotate_token_counts,
    group_messages,
    included_token_count,
    set_excluded,
)
from test_anchored import TOKENIZER, _conversation
from test_record_budget import _history, _sent, _sent_values, _size, _strategy, _Summarizer
from test_record_reasoning import _retain_later_reasoning

from maf_compaction import _anchored as anchored_module
from maf_compaction import _record_replay as replay_module
from maf_compaction._anchored import AnchoredCompactionStrategy
from maf_compaction._composed import (
    DEFAULT_RECORD_MERGE_PROMPT,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    _transcript,
)
from maf_compaction._preserve import STANDS_IN_KEY
from maf_compaction._record_replay import LINEAGE_KEY, MessageKeyCache, _message_key
from maf_compaction._toolsummary import (
    _reasoning_prefix_yields_to_budget,
    consolidatable_record_groups,
)

_TOOL_CALL = "tool_call"
_ASSISTANT_TEXT = "assistant_text"
_HOST_REASON = "host"

#: Records and shed narration in the applied history: enough of both that work repeated per
#: accepted edit, or per edit and shed message, stands well clear of work done once.
_RECORDS = 30
_NARRATION = 40


class _Noteless(AnchoredCompactionStrategy):
    """Sheds without leaving notes, so a count after the call is the count the loop ended on."""

    def _insert_note(self, messages: list[Message], group: dict[str, Any], note: str) -> None:
        return None


def _anchored() -> AnchoredCompactionStrategy:
    return _Noteless(max_input_tokens=1_000_000, tokenizer=TOKENIZER, keep_tokens=1_000_000)


def _annotated(messages: list[Message]) -> list[Message]:
    annotate_message_groups(messages)
    annotate_token_counts(messages, tokenizer=TOKENIZER)
    return messages


def _tokens(message: Message) -> int:
    return included_token_count([message])


def _narrated(turns: int) -> list[Message]:
    """Return turns of one question and one reply too long to be kept as a short answer."""
    messages = [
        Message(role="system", contents=["You are an assistant."], message_id="sys"),
        Message(role="user", contents=["Requirement: region is EU-WEST-1."], message_id="u0"),
        Message(role="assistant", contents=["Understood."], message_id="a0"),
    ]
    for index in range(turns):
        messages.append(Message(role="user", contents=[f"Ask {index}."], message_id=f"u_{index}"))
        messages.append(
            Message(
                role="assistant",
                contents=[f"Reply {index}. " + "detail " * (60 + index)],
                message_id=f"a_{index}",
            )
        )
    return messages


def _shed_watched(
    monkeypatch: pytest.MonkeyPatch,
    strategy: AnchoredCompactionStrategy,
    messages: list[Message],
    kind: str,
    ceiling: int,
) -> list[int]:
    """Shed, and return the recounted total as it stood before each group was dropped."""
    before_drop: list[int] = []
    seen: set[str] = set()

    def watched(message: Message, *, excluded: bool, reason: str | None = None) -> bool:
        group = message.additional_properties[GROUP_ANNOTATION_KEY]["id"]
        if excluded and group not in seen:
            seen.add(group)
            before_drop.append(included_token_count(messages))
        return set_excluded(message, excluded=excluded, reason=reason)

    monkeypatch.setattr(anchored_module, "set_excluded", watched)
    strategy._shed(messages, kind, ceiling=ceiling)
    monkeypatch.setattr(anchored_module, "set_excluded", set_excluded)
    return before_drop


def _assert_shed_as_a_recount_would(
    strategy: AnchoredCompactionStrategy,
    messages: list[Message],
    kind: str,
    ceiling: int,
    before_drop: list[int],
) -> None:
    """The stop test, stated on recounts: drop only while over, and never stop while over."""
    assert all(total > ceiling for total in before_drop), "a group was dropped under the ceiling"
    if included_token_count(messages) > ceiling:
        assert not strategy._shed(messages, kind, ceiling=ceiling), "it stopped over the ceiling"


@pytest.mark.parametrize("fraction", [0.0, 0.25, 0.5, 0.75, 0.95, 1.0])
@pytest.mark.parametrize("kind", [_TOOL_CALL, _ASSISTANT_TEXT])
def test_shedding_on_a_running_total_stops_where_a_recount_would(
    monkeypatch: pytest.MonkeyPatch, kind: str, fraction: float
) -> None:
    messages = _annotated(
        _conversation(tool_turns=12, payload_chars=400)
        if kind == _TOOL_CALL
        else _narrated(turns=12)
    )
    strategy = _anchored()
    ceiling = int(included_token_count(messages) * fraction)

    before_drop = _shed_watched(monkeypatch, strategy, messages, kind, ceiling)

    assert bool(before_drop) == (fraction < 1.0)
    _assert_shed_as_a_recount_would(strategy, messages, kind, ceiling, before_drop)


def test_a_partly_excluded_group_leaves_the_total_by_its_included_members_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A result the host excluded is not in the total, so dropping its group cannot remove it."""
    messages = _conversation(tool_turns=12, payload_chars=400)
    result = next(message for message in messages if message.message_id == "t_res_0")
    call = next(message for message in messages if message.message_id == "a_call_0")
    set_excluded(result, excluded=True, reason=_HOST_REASON)
    _annotated(messages)
    strategy = _anchored()
    # Under the ceiling only if the excluded result is taken off the total with its call.
    ceiling = included_token_count(messages) - _tokens(call) - 1

    before_drop = _shed_watched(monkeypatch, strategy, messages, _TOOL_CALL, ceiling)

    assert len(before_drop) == 2, "the partly excluded group, and one more to reach the ceiling"
    assert call.additional_properties[EXCLUDED_KEY]
    _assert_shed_as_a_recount_would(strategy, messages, _TOOL_CALL, ceiling, before_drop)


@pytest.mark.parametrize("shared", ["message", "properties"])
def test_a_list_sharing_an_exclusion_flag_is_recounted(
    monkeypatch: pytest.MonkeyPatch, shared: str
) -> None:
    """One flag excluding two entries takes both off the total, which only a recount sees.

    Such a group is not removable today: its two spans carry one group id. The total must not
    depend on that rule, so it is lifted here.
    """
    messages = _narrated(turns=8)
    first = next(index for index, m in enumerate(messages) if m.message_id == "a_1")
    second = next(index for index, m in enumerate(messages) if m.message_id == "a_2")
    if shared == "message":
        messages[second] = messages[first]
    else:
        messages[second] = copy.copy(messages[first])
        messages[second].additional_properties = messages[first].additional_properties
    _annotated(messages)
    monkeypatch.setattr(
        anchored_module,
        "removable_spans",
        lambda _, spans: {(span["start_index"], span["end_index"]) for span in spans},
    )
    strategy = _anchored()
    oldest = next(m for m in messages if m.message_id == "a_0")
    # Met once the shared flag is set, and missed by the tokens of one of its two entries.
    ceiling = (included_token_count(messages) - _tokens(oldest) - 2 * _tokens(messages[first])) + 1

    before_drop = _shed_watched(monkeypatch, strategy, messages, _ASSISTANT_TEXT, ceiling)

    assert len(before_drop) == 2, "the oldest reply and the shared one, and nothing behind them"
    assert included_token_count(messages) <= ceiling
    _assert_shed_as_a_recount_would(strategy, messages, _ASSISTANT_TEXT, ceiling, before_drop)


def test_a_recounting_shed_skips_excluded_groups_before_it_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Groups shed on earlier calls are most of a long band, and cost a recount nothing."""
    messages = _narrated(turns=12)
    shared = next(index for index, m in enumerate(messages) if m.message_id == "a_8")
    # One reply listed twice: neither entry is removable, and the list is recounted.
    messages[shared + 2] = messages[shared]
    _annotated(messages)
    strategy = _anchored()
    assert strategy._shed(messages, _ASSISTANT_TEXT, ceiling=0)
    band = strategy._middle_band(messages, group_messages(messages))
    live = [
        group
        for group in band
        if group["kind"] == _ASSISTANT_TEXT
        and not messages[group["start_index"]].additional_properties.get(EXCLUDED_KEY)
    ]
    assert len(band) > 3 * len(live) > 0, "the premise: nearly every reply is already shed"
    counts: list[int] = []

    def counted(counted_messages: list[Message]) -> int:
        counts.append(len(counted_messages))
        return included_token_count(counted_messages)

    monkeypatch.setattr(anchored_module, "included_token_count", counted)

    assert not strategy._shed(messages, _ASSISTANT_TEXT, ceiling=0)

    assert counts == [len(messages)] * (1 + len(live))


def test_a_shed_counts_the_conversation_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The total is counted on entry; each drop then counts its own group and nothing else."""
    messages = _annotated(_conversation(tool_turns=40, payload_chars=200))
    strategy = _anchored()
    counts: list[int] = []

    def counted(counted_messages: list[Message]) -> int:
        counts.append(len(counted_messages))
        return included_token_count(counted_messages)

    monkeypatch.setattr(anchored_module, "included_token_count", counted)

    assert strategy._shed(messages, _TOOL_CALL, ceiling=0)

    dropped = sum(1 for m in messages if m.additional_properties.get(EXCLUDED_KEY)) // 2
    assert dropped > 30, "the premise: many groups were shed in the one call"
    assert counts[0] > 100 and all(size == 2 for size in counts[1:])
    assert len(counts) == 1 + dropped


def test_shedding_to_a_ceiling_counts_once_per_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each round re-measures, as notes cost tokens, but no round counts once per group."""
    messages = _conversation(tool_turns=40, payload_chars=200)
    strategy = AnchoredCompactionStrategy(
        max_input_tokens=1_000_000, tokenizer=TOKENIZER, keep_tokens=1_000_000
    )
    whole = 0
    sheds = 0
    shed = AnchoredCompactionStrategy._shed

    def counted(counted_messages: list[Message]) -> int:
        nonlocal whole
        whole += counted_messages is messages
        return included_token_count(counted_messages)

    def shed_counted(self: Any, *args: Any, **kwargs: Any) -> bool:
        nonlocal sheds
        sheds += 1
        return shed(self, *args, **kwargs)

    monkeypatch.setattr(anchored_module, "included_token_count", counted)
    monkeypatch.setattr(AnchoredCompactionStrategy, "_shed", shed_counted)

    assert strategy.shed_to(messages, ceiling=0)

    assert sheds >= 2
    # A round's own two checks, and one count on entry to each shed it runs.
    assert whole <= 2 * sheds + 1


async def _applied(
    records: int = _RECORDS, narration: int = _NARRATION
) -> tuple[
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    _Summarizer,
    list[Message],
    list[Message],
]:
    """Return a history carrying a chain of accepted merges, with narration shed after them.

    Each merge takes the previous one's record with two more, so every edit's window holds the
    output of the one before. The narration is shed once all are accepted: replay rebuilds each
    edit's window as it stood, without that shedding.

    Returns:
        The strategy, its summarizer, the applied history, and the history as first handed.
    """
    messages = _history(records)
    for index in range(narration):
        messages[2:2] = [
            Message("user", [f"Question {index}."], message_id=f"question{index}"),
            Message("assistant", [f"Narration {index}. " * 20], message_id=f"narration{index}"),
        ]
    handed = copy.deepcopy(messages)
    summarizer = _Summarizer()
    strategy = _strategy(summarizer, budget=1_000_000, cap=None)
    _size(messages)
    assert strategy._reshape_records(messages)
    while len(groups := consolidatable_record_groups(messages)[:3]) == 3:
        assert (
            await strategy._consolidate(
                messages,
                groups,
                prompt=DEFAULT_RECORD_MERGE_PROMPT,
                transcript=_transcript(messages, groups),
            )
            == "accepted"
        )
    strategy._shed_ids.update(f"narration{index}" for index in range(narration))
    assert strategy._keep_shedding(messages)
    for history in (messages, handed):
        _retain_later_reasoning(history)
    assert len(strategy._record_order) > records // 3
    return strategy, summarizer, messages, handed


def _state(
    strategy: ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    messages: list[Message],
) -> tuple[str, str]:
    """Return everything a pass could have changed: every message, and the decisions kept."""
    return (
        json.dumps([message.to_dict() for message in messages], sort_keys=True, default=str),
        json.dumps(strategy.session_state(), sort_keys=True, default=str),
    )


def _counting(monkeypatch: pytest.MonkeyPatch, owner: Any, name: str, calls: Counter[str]) -> None:
    original = getattr(owner, name)

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls[name] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, counted)


@pytest.mark.anyio
async def test_a_steady_pass_replays_once_per_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three entry points replay, each in one traversal; no accepted edit adds another."""
    strategy, summarizer, messages, _ = await _applied()
    await strategy(messages)
    asked = len(summarizer.requests)
    calls: Counter[str] = Counter()
    composed = ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
    _counting(monkeypatch, composed, "_replay_record_edits", calls)
    _counting(monkeypatch, composed, "_replay_record_candidates", calls)

    assert not await strategy(messages)

    assert len(summarizer.requests) == asked
    assert calls["_replay_record_edits"] == 3
    # The private preview finds nothing to commit, so no second traversal follows it.
    assert calls["_replay_record_candidates"] == 3


@pytest.mark.anyio
async def test_a_steady_pass_fingerprints_each_message_a_bounded_number_of_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fingerprints are shared by a replay call's views, however many edits rebuild a window."""
    strategy, _, messages, _ = await _applied()
    await strategy(messages)
    calls: Counter[str] = Counter()
    _counting(monkeypatch, replay_module, "_message_key", calls)

    assert not await strategy(messages)

    # Two full replays a pass, and a message read in at most two exclusion states by each.
    assert 0 < calls["_message_key"] <= 4 * len(messages)


@pytest.mark.anyio
async def test_fingerprints_do_not_grow_with_the_edits_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A longer chain of edits over the same shed narration costs no more per message."""
    per_message: list[float] = []
    for records in (12, _RECORDS):
        strategy, _, messages, _ = await _applied(records=records)
        await strategy(messages)
        calls: Counter[str] = Counter()
        with monkeypatch.context() as patch:
            _counting(patch, replay_module, "_message_key", calls)
            assert not strategy._replay_record_edits(messages)
        per_message.append(calls["_message_key"] / len(messages))

    assert per_message[1] <= per_message[0] * 1.25


@pytest.mark.anyio
async def test_replay_on_an_applied_history_changes_nothing() -> None:
    strategy, _, messages, _ = await _applied()
    await strategy(messages)
    before = _state(strategy, messages)

    assert not strategy._replay_record_edits(messages)
    assert not strategy._replay_record_edits(messages, reshape_only=True)
    assert not strategy._keep_decisions(messages)

    assert _state(strategy, messages) == before


@pytest.mark.anyio
async def test_one_replay_of_a_handed_history_is_a_fixpoint() -> None:
    """A history handed without its replacements takes them in one replay; a second is idle."""
    strategy, _, messages, handed = await _applied()
    cold = _strategy(_Summarizer(), budget=1_000_000, cap=None)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    _size(handed)

    assert cold._replay_record_edits(handed)
    assert cold._keep_shedding(handed)
    after = _state(cold, handed)

    assert _sent(handed) == _sent(messages)
    assert not cold._replay_record_edits(handed)
    assert _state(cold, handed) == after


@pytest.mark.anyio
async def test_two_passes_over_the_same_history_agree() -> None:
    strategy, summarizer, messages, _ = await _applied()
    await strategy(messages)
    asked = len(summarizer.requests)
    first = _state(strategy, messages), _sent(messages)

    assert not await strategy(messages)

    assert (_state(strategy, messages), _sent(messages)) == first
    assert len(summarizer.requests) == asked


@pytest.mark.anyio
@pytest.mark.parametrize("retained_reasoning", [False, True])
async def test_a_merge_that_recreates_a_replaced_record_takes_its_replacement(
    retained_reasoning: bool,
) -> None:
    """A merge that comes back as a record an earlier rewrite replaced is rewritten, unasked.

    The rewrite's window holds the merged record, so it matches nothing until the merge is
    made again. Nothing replays between the merge and the next thing the chain asks, so the
    replacement is taken by transcript, where the merge was accepted.
    """
    messages = _history(3)
    handed = copy.deepcopy(messages)
    merged = " ".join(sorted(_sent_values(messages)))
    answers = iter([merged + " Additional details." * 20, merged])
    strategy = _strategy(_Summarizer(merge=lambda _: next(answers)), budget=1_000_000, cap=None)
    for _ in range(2):
        groups = consolidatable_record_groups(_sized(messages))
        assert (
            await strategy._consolidate(
                messages,
                groups,
                prompt=DEFAULT_RECORD_MERGE_PROMPT,
                transcript=_transcript(messages, groups),
            )
            == "accepted"
        )
    if retained_reasoning:
        for history in (messages, handed):
            _retain_later_reasoning(history)
    summarizer = _Summarizer(merge=lambda _: merged + " Additional details." * 20)
    cold = _strategy(summarizer, budget=1_000_000, cap=None)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    replays = 0
    replay = cold._replay_record_edits

    def counted(*args: Any, **kwargs: Any) -> bool:
        nonlocal replays
        replays += 1
        return replay(*args, **kwargs)

    cold._replay_record_edits = counted  # type: ignore[method-assign]

    # The chain merges a prefix that retained reasoning follows only while it yields to it.
    with _reasoning_prefix_yields_to_budget():
        groups = consolidatable_record_groups(_sized(handed))
        assert len(groups) == 3
        assert (
            await cold._consolidate(
                handed,
                groups,
                prompt=DEFAULT_RECORD_MERGE_PROMPT,
                transcript=_transcript(handed, groups),
            )
            == "accepted"
        )

    assert not replays
    assert len(summarizer.requests) == 1, "the merge was asked; the rewrite was already decided"
    assert _sent(handed) == _sent(messages)
    assert len(cold._record_order) == 2


def _sized(messages: list[Message]) -> list[Message]:
    _size(messages)
    return messages


def _variants(message: Message) -> list[Message]:
    """Return copies sharing ``message``'s contents, each differing in one fingerprinted field."""
    changes: list[dict[str, Any]] = [
        {},
        {"role": "user"},
        {"author_name": "another"},
        {"message_id": "another-id"},
        {"message_id": None},
    ]
    variants: list[Message] = []
    for change in changes:
        variant = copy.copy(message)
        variant.additional_properties = dict(message.additional_properties)
        for name, value in change.items():
            setattr(variant, name, value)
        variants.append(variant)
    for key, value in ((EXCLUDED_KEY, True), (STANDS_IN_KEY, "call:0"), (LINEAGE_KEY, "abc")):
        variant = copy.copy(message)
        variant.additional_properties = {**message.additional_properties, key: value}
        variants.append(variant)
    return variants


def test_the_key_cache_tells_apart_everything_a_fingerprint_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copies share their contents, so every other field has to be part of the cache's key."""
    message = Message("assistant", ["A record body."], message_id="record")
    variants = _variants(message)
    assert all(variant.contents is message.contents for variant in variants)
    expected = [_message_key(variant) for variant in variants]
    assert len(set(expected)) == len(variants), "the premise: each variant reads differently"
    cache = MessageKeyCache()
    calls: Counter[str] = Counter()
    _counting(monkeypatch, replay_module, "_message_key", calls)

    assert [cache.key(variant) for variant in variants] == expected
    assert [cache.key(variant) for variant in reversed(variants)] == expected[::-1]

    assert calls["_message_key"] == len(variants), "and the second reading was all cache"


@pytest.mark.parametrize("field", ["role", "author_name", STANDS_IN_KEY, LINEAGE_KEY])
def test_the_key_cache_fingerprints_afresh_what_it_cannot_key(field: str) -> None:
    """``1`` and ``True`` are one dictionary key and two serializations; neither is cached."""
    readings: list[str] = []
    cache = MessageKeyCache()
    contents: list[Any] = ["A record body."]
    for value in (1, True):
        message = Message("assistant", contents, message_id="record")
        message.contents = contents = message.contents
        if field in {STANDS_IN_KEY, LINEAGE_KEY}:
            message.additional_properties[field] = value
        else:
            setattr(message, field, value)
        assert cache.key(message) == _message_key(message)
        readings.append(cache.key(message))

    assert readings[0] != readings[1]
    assert not cache.entries


@pytest.mark.parametrize("change", ["replaced", "appended", "removed"])
def test_the_key_cache_rereads_a_contents_list_that_changed(change: str) -> None:
    message = Message("assistant", ["First.", "Second."], message_id="record")
    cache = MessageKeyCache()
    before = cache.key(message)
    held = list(message.contents)
    if change == "replaced":
        message.contents[1] = Content.from_text("Another.")
    elif change == "appended":
        message.contents.append(Content.from_text("Third."))
    else:
        del message.contents[1]

    assert cache.key(message) == _message_key(message) != before
    assert held, "the first contents outlive the change, so no new content can take their ids"


def test_the_key_cache_holds_the_contents_it_read() -> None:
    """An entry is found by the list's identity, which is only its own while the list lives."""
    cache = MessageKeyCache()
    message = Message("assistant", ["A record body."], message_id="record")
    contents, items = message.contents, list(message.contents)
    cache.key(message)

    ((held_list, held_items, _),) = cache.entries.values()

    assert held_list is contents
    assert len(held_items) == len(items) and all(a is b for a, b in zip(held_items, items))


@pytest.mark.anyio
async def test_every_cached_fingerprint_is_the_one_a_fresh_reading_gives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Across a replay that rebuilds windows, restores and re-sheds, no cached answer is stale."""
    strategy, _, messages, handed = await _applied(records=12, narration=8)
    cold = _strategy(_Summarizer(), budget=1_000_000, cap=None)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    key = MessageKeyCache.key
    answers: Counter[str] = Counter()

    def checked(self: MessageKeyCache, message: Message) -> str:
        known = len(self.entries)
        answer = key(self, message)
        answers["cached" if len(self.entries) == known else "read"] += 1
        assert answer == _message_key(message)
        return answer

    monkeypatch.setattr(MessageKeyCache, "key", checked)

    assert await cold(handed)
    assert not await cold(handed)

    assert _sent(handed) == _sent(messages)
    assert answers["cached"] and answers["read"]


@pytest.mark.anyio
async def test_no_fingerprint_outlives_the_replay_that_read_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Results are rewritten in place between replays, which no identity would show."""
    strategy, _, messages, _ = await _applied(records=12, narration=8)
    await strategy(messages)
    calls: Counter[str] = Counter()
    _counting(monkeypatch, replay_module, "_message_key", calls)

    assert not strategy._replay_record_edits(messages)
    first = calls["_message_key"]
    assert not strategy._replay_record_edits(messages)

    assert first > 0 and calls["_message_key"] == 2 * first
    caches = [value for value in vars(strategy).values() if isinstance(value, MessageKeyCache)]
    assert not caches
