"""What putting saved record edits back costs on a history handed without them.

A host that rebuilds its transcript for every run hands the first pass of a run the original
messages and the strategy its saved decisions, so that pass puts every accepted edit back.
Each edit rebuilds its window, and the work under that loop is what a long conversation pays
once per edit: these tests bound it in counts -- traversals, serializations, token counts,
walks of the history -- and pin that the cheaper forms answer as the older ones did.
"""

from __future__ import annotations

import copy
import json
import random
from collections import Counter
from typing import Any

import pytest
from agent_framework import Content, Message
from agent_framework._compaction import (
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
    annotate_message_groups,
    group_messages,
    set_excluded,
)
from agent_framework._compaction import annotate_token_counts as framework_annotate_token_counts
from test_pass_cost import _applied, _counting, _state
from test_record_budget import TOKENIZER, _sent, _size, _strategy, _Summarizer

from maf_compaction import _composed as composed_module
from maf_compaction import _record_replay as replay_module
from maf_compaction import _shed as shed_module
from maf_compaction import _usersummary as usersummary_module
from maf_compaction._composed import ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
from maf_compaction._media import TokenCounts, annotate_token_counts
from maf_compaction._record_replay import (
    LINEAGE_KEY,
    MessageKeyCache,
    ReplayUndo,
    ReplayView,
    replacement_identity,
)
from maf_compaction._shed import ShedGroupKeys, unique_shed_groups
from maf_compaction._toolsummary import (
    _reasoning_prefix_yields_to_budget,
    consolidatable_record_groups,
)

_Composed = ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy

#: Shed narration in the handed history: every edit's window is rebuilt without it.
_NARRATION = 40


async def _handed(
    records: int,
) -> tuple[_Composed, list[Message], list[tuple[str, list[dict[str, Any]]]]]:
    """Return a restored strategy and a rebuilt history as a pass holds them before its replay.

    The history is the one first handed, without any replacement. The pass has shed again what
    earlier passes shed, half of it found by content as a group with a positional id is, and
    has put the reshapes back, so the history holds written records and every merge is still
    to be put back on a window rebuilt without that shedding.

    Returns:
        The strategy, the history, and what the model is sent once everything is back.
    """
    strategy, _, messages, handed = await _applied(records=records, narration=_NARRATION)
    cold = _strategy(_Summarizer(), budget=1_000_000, cap=None)
    cold.restore_session_state(json.loads(json.dumps(strategy.session_state())))
    _size(handed)
    by_content = {
        key
        for key, members in unique_shed_groups(handed)
        if (members[0].message_id or "").startswith("narration")
        and int(str(members[0].message_id).removeprefix("narration")) % 2
    }
    assert by_content
    cold._shed_groups = set(by_content)
    cold._shed_ids = {
        identity for identity in cold._shed_ids if not int(identity.removeprefix("narration")) % 2
    }
    assert cold._keep_shedding(handed)
    assert cold._reshape_records(handed, replay=True)
    assert any(replacement_identity(message) for message in handed)
    return cold, handed, _sent(messages)


def _finished(cold: _Composed, handed: list[Message]) -> list[tuple[str, list[dict[str, Any]]]]:
    cold._keep_shedding(handed)
    return _sent(handed)


@pytest.mark.anyio
@pytest.mark.parametrize("records", [12, 30])
async def test_a_handed_history_takes_its_edits_back_in_one_traversal(
    monkeypatch: pytest.MonkeyPatch, records: int
) -> None:
    """The traversal that learns the chain can be committed is the one that commits it."""
    cold, handed, expected = await _handed(records)
    calls: Counter[str] = Counter()
    _counting(monkeypatch, _Composed, "_replay_record_candidates", calls)

    assert cold._replay_record_edits(handed)

    assert calls["_replay_record_candidates"] == 1
    assert _finished(cold, handed) == expected
    assert not cold._replay_record_edits(handed)


@pytest.mark.anyio
async def test_serializations_do_not_grow_with_the_edits_put_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fingerprints, group keys and token counts are each taken once for the whole replay."""
    per_message: list[float] = []
    for records in (12, 30):
        cold, handed, expected = await _handed(records)
        calls: Counter[str] = Counter()
        with monkeypatch.context() as patch:
            _counting(patch, Content, "to_dict", calls)
            assert cold._replay_record_edits(handed)
        assert _finished(cold, handed) == expected
        per_message.append(calls["to_dict"] / len(handed))

    # A message is read for its fingerprint in two exclusion states, its group and its count.
    assert all(0 < count <= 5 for count in per_message)
    assert per_message[1] <= per_message[0] * 1.5


@pytest.mark.anyio
@pytest.mark.parametrize("records", [12, 30])
async def test_the_history_behind_a_record_put_back_is_counted_once(
    monkeypatch: pytest.MonkeyPatch, records: int
) -> None:
    """Each record inserted would have every message behind it serialized and counted again."""
    cold, handed, expected = await _handed(records)
    calls: Counter[str] = Counter()
    _counting(monkeypatch, type(TOKENIZER), "count_tokens", calls)

    assert cold._replay_record_edits(handed)

    assert len(cold._record_order) > records // 3
    assert 0 < calls["count_tokens"] <= len(handed)
    assert _finished(cold, handed) == expected


@pytest.mark.anyio
@pytest.mark.parametrize("records", [12, 30])
async def test_shed_groups_are_fingerprinted_once_for_all_the_edits_put_back(
    monkeypatch: pytest.MonkeyPatch, records: int
) -> None:
    """Every window asks which groups were shed by content; the answer is not serialized anew."""
    cold, handed, expected = await _handed(records)
    serialized = 0
    looking = False
    lookup = unique_shed_groups
    to_dict = Content.to_dict

    def looked_up(*args: Any, **kwargs: Any) -> Any:
        nonlocal looking
        looking = True
        try:
            return lookup(*args, **kwargs)
        finally:
            looking = False

    def counted(self: Content, *args: Any, **kwargs: Any) -> Any:
        nonlocal serialized
        serialized += looking
        return to_dict(self, *args, **kwargs)

    monkeypatch.setattr(replay_module, "unique_shed_groups", looked_up)
    monkeypatch.setattr(Content, "to_dict", counted)

    assert cold._replay_record_edits(handed)

    contents = sum(len(message.contents) for message in handed)
    assert 0 < serialized <= contents * 1.2
    monkeypatch.undo()
    assert _finished(cold, handed) == expected


@pytest.mark.anyio
async def test_the_reshape_index_is_built_for_the_edits_that_read_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an edit at or before the last reshape expects a stand-in; later ones build none."""
    counts: list[Counter[str]] = []
    for records in (12, 30):
        cold, handed, expected = await _handed(records)
        reshapes = [index for index, edit in enumerate(cold._record_order) if edit.text is None]
        assert reshapes == [0], "the premise: every merge was accepted after the last reshape"
        calls: Counter[str] = Counter()
        with monkeypatch.context() as patch:
            _counting(patch, composed_module, "reshapeable_record_groups", calls)
            _counting(patch, ReplayView, "fork", calls)
            assert cold._replay_record_edits(handed)
        assert _finished(cold, handed) == expected
        counts.append(calls)

    assert counts[1]["reshapeable_record_groups"] <= counts[0]["reshapeable_record_groups"] <= 2
    assert counts[1]["fork"] <= counts[0]["fork"] <= 2


@pytest.mark.anyio
@pytest.mark.parametrize("records", [12, 30])
async def test_merges_taken_back_by_transcript_read_the_history_once(
    monkeypatch: pytest.MonkeyPatch, records: int
) -> None:
    """A chain the exact replay refuses is redone here, one merge after another."""
    cold, handed, expected = await _handed(records)
    edits = len(cold._record_order)
    calls: Counter[str] = Counter()
    _counting(monkeypatch, Content, "to_dict", calls)
    _counting(monkeypatch, type(TOKENIZER), "count_tokens", calls)

    # As the chain yields the reasoning prefix when it fits a prompt over its budget.
    with _reasoning_prefix_yields_to_budget():
        assert cold._keep_records(handed)

    assert len(cold._record_order) - edits > records // 3, "the premise: many merges were redone"
    assert 0 < calls["count_tokens"] <= len(handed)
    assert 0 < calls["to_dict"] <= 3 * len(handed)
    monkeypatch.undo()
    assert _finished(cold, handed) == expected


@pytest.mark.anyio
async def test_available_members_are_read_from_the_fingerprints_of_the_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every shed message is fingerprinted as if included, once however often the history changes."""
    strategy, _, messages, _ = await _applied(records=12, narration=8)
    keys = MessageKeyCache()
    members = strategy._available_record_members(ReplayView(messages, key_cache=keys))
    clones = [copy.copy(message) for message in messages]
    for clone in clones:
        clone.additional_properties = dict(clone.additional_properties)
    calls: Counter[str] = Counter()
    _counting(monkeypatch, replay_module, "_message_key", calls)

    again = strategy._available_record_members(ReplayView(clones, key_cache=keys))

    assert again == members and len(members) > 8
    assert not calls


@pytest.mark.anyio
async def test_a_record_put_back_reads_its_lineage_from_the_fingerprints_of_the_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cold, handed, _ = await _handed(12)
    keys = MessageKeyCache()
    for message in handed:
        keys.key(message)
    with _reasoning_prefix_yields_to_budget():
        groups = consolidatable_record_groups(handed)[:3]
    calls: Counter[str] = Counter()
    _counting(monkeypatch, replay_module, "_message_key", calls)

    cold.tool_results.consolidate_records(handed, groups, "Merged.", replay=True, keys=keys)

    assert len(groups) == 3
    assert not calls
    assert any(m.additional_properties.get(LINEAGE_KEY) for m in handed)


@pytest.mark.anyio
@pytest.mark.parametrize("records", [12, 30])
async def test_user_turn_fingerprints_outlive_each_edit_put_back(
    monkeypatch: pytest.MonkeyPatch, records: int
) -> None:
    """A user turn's fingerprint is of the turn, and each edit put back changes only the history."""
    cold, handed, _ = await _handed(records)
    for index in range(6):
        group = next(
            span
            for span in group_messages(handed)
            if handed[span["start_index"]].message_id == f"question{index}"
        )
        cold.user_turns._replace(
            handed, [group], f"Summary {index}.", summary_id=f"summary-{index}"
        )
    calls: Counter[str] = Counter()
    _counting(monkeypatch, replay_module, "media_identity", calls)

    assert cold._replay_record_edits(handed)

    users = sum(1 for message in handed if message.role == "user")
    assert 0 < calls["media_identity"] <= 2 * users
    assert not cold._replay_record_edits(handed)


@pytest.mark.parametrize("summaries", [3, 9])
def test_held_summaries_are_put_back_on_one_reading_of_the_groups(
    monkeypatch: pytest.MonkeyPatch, summaries: int
) -> None:
    """Every held summary's window is found in the same spans until one is inserted."""
    messages = [
        Message("user", [f"Facts {index}. " * 50], message_id=f"source-{index}")
        for index in range(summaries)
    ]
    messages.append(Message("user", ["The live request."], message_id="live"))
    strategy = _strategy(_Summarizer(), budget=100_000)
    _size(messages)
    for index in range(summaries):
        group = next(
            span
            for span in group_messages(messages)
            if messages[span["start_index"]].message_id == f"source-{index}"
        )
        strategy.user_turns._replace(
            messages, [group], f"Summary {index}.", summary_id=f"summary-{index}"
        )
    before = [message.to_dict() for message in messages]
    calls: Counter[str] = Counter()
    _counting(monkeypatch, usersummary_module, "group_messages", calls)

    strategy.user_turns.replay_summaries(messages, restore_held_sources=True)

    assert calls["group_messages"] == 1
    assert [message.to_dict() for message in messages] == before


@pytest.mark.anyio
async def test_the_single_traversal_leaves_what_the_previewed_one_does() -> None:
    """A committed replay is the second of the two traversals it replaced, and nothing else."""
    states: list[tuple[bool, tuple[str, str]]] = []
    for previewed in (False, True):
        cold, handed, _ = await _handed(12)
        changed = (
            cold._replay_record_edits_previewed(handed, False, MessageKeyCache())
            if previewed
            else cold._replay_record_edits(handed)
        )
        states.append((changed, _state(cold, handed)))

    assert states[0][0] and states[0] == states[1]


@pytest.mark.anyio
@pytest.mark.parametrize("refusal", ["placement", "error"])
async def test_a_replay_refused_part_way_leaves_no_trace(
    monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    """Edits already put back when the chain is refused are taken back with everything else."""
    cold, handed, _ = await _handed(12)
    before = _state(cold, handed)
    objects = list(handed)
    properties = [(message.additional_properties, dict(vars(message))) for message in handed]
    applied = 0
    consolidate = type(cold.tool_results).consolidate_records

    def consolidated(*args: Any, **kwargs: Any) -> None:
        nonlocal applied
        applied += 1
        if refusal == "error" and applied == 3:
            raise RuntimeError("stop")
        consolidate(*args, **kwargs)

    def positions(*_: Any) -> bool:
        return applied < 2

    monkeypatch.setattr(type(cold.tool_results), "consolidate_records", consolidated)
    if refusal == "placement":
        monkeypatch.setattr(_Composed, "_record_replacement_positions", staticmethod(positions))
        assert not cold._replay_record_edits(handed)
    else:
        with pytest.raises(RuntimeError, match="stop"):
            cold._replay_record_edits(handed)

    assert applied >= 2, "the premise: edits had been put back before the refusal"
    assert _state(cold, handed) == before
    assert len(handed) == len(objects)
    assert all(now is then for now, then in zip(handed, objects, strict=True))
    assert all(
        message.additional_properties is kept and vars(message) == attributes
        for message, (kept, attributes) in zip(handed, properties, strict=True)
    )


@pytest.mark.anyio
@pytest.mark.parametrize("shared", ["properties", "annotation"])
async def test_a_history_sharing_message_state_is_replayed_on_copies_first(
    monkeypatch: pytest.MonkeyPatch, shared: str
) -> None:
    """Copies give each message its own state, so only they tell two such messages apart."""
    cold, handed, _ = await _handed(12)
    first, second = handed[0], handed[1]
    if shared == "properties":
        second.additional_properties = first.additional_properties
    else:
        second.additional_properties[GROUP_ANNOTATION_KEY] = first.additional_properties[
            GROUP_ANNOTATION_KEY
        ]
    assert ReplayUndo.take(handed) is None
    calls: Counter[str] = Counter()
    _counting(monkeypatch, _Composed, "_replay_record_edits_previewed", calls)

    cold._replay_record_edits(handed)

    assert calls["_replay_record_edits_previewed"] == 1


def _conversation(count: int) -> list[Message]:
    messages = [Message("user", ["Task."], message_id="task")]
    for index in range(count):
        messages.append(Message("assistant", [f"Reply {index}."], message_id=f"reply{index}"))
        messages.append(Message("user", [f"Ask {index}."], message_id=f"ask{index}"))
    annotate_message_groups(messages)
    return messages


def test_an_undone_history_is_the_one_that_was_kept() -> None:
    """Order, attributes, properties and annotations all go back, on the objects handed."""
    messages = _conversation(6)
    stored = copy.copy(messages[3])
    stored.additional_properties = dict(messages[3].additional_properties)
    assert (
        stored.additional_properties[GROUP_ANNOTATION_KEY]
        is messages[3].additional_properties[GROUP_ANNOTATION_KEY]
    )
    before = [(message, message.to_dict(), message.additional_properties) for message in messages]
    annotations = [message.additional_properties[GROUP_ANNOTATION_KEY] for message in messages]
    shared_before = dict(stored.additional_properties[GROUP_ANNOTATION_KEY])
    undo = ReplayUndo.take(messages)
    assert undo is not None

    set_excluded(messages[1], excluded=True, reason="test")
    messages[2].message_id = "renamed"
    messages[3].additional_properties[GROUP_ANNOTATION_KEY]["token_count"] = 99
    messages[4].additional_properties[GROUP_ANNOTATION_KEY] = {"id": "another"}
    messages[5].additional_properties = {"replaced": True}
    messages.insert(2, Message("assistant", ["Inserted."], message_id="inserted"))
    del messages[-1]
    messages.reverse()
    undo.restore()

    assert len(messages) == len(before)
    for now, (then, state, properties), annotation in zip(
        messages, before, annotations, strict=True
    ):
        assert now is then and now.to_dict() == state
        assert now.additional_properties is properties
        assert now.additional_properties[GROUP_ANNOTATION_KEY] is annotation
    assert stored.additional_properties[GROUP_ANNOTATION_KEY] == shared_before


@pytest.mark.parametrize("shared", ["properties", "annotation", "listed-twice"])
def test_a_history_is_kept_only_if_copies_would_mirror_it(shared: str) -> None:
    messages = _conversation(3)
    if shared == "properties":
        messages[2].additional_properties = messages[1].additional_properties
    elif shared == "annotation":
        messages[2].additional_properties[GROUP_ANNOTATION_KEY] = messages[1].additional_properties[
            GROUP_ANNOTATION_KEY
        ]
    else:
        messages.append(messages[1])

    undo = ReplayUndo.take(messages)

    # One message listed twice is one copy listed twice; two sharing state become two.
    assert (undo is not None) == (shared == "listed-twice")


class _Walked(list[Message]):
    """A history that counts how often it is read from the start."""

    walks = 0

    def __iter__(self) -> Any:
        self.walks += 1
        return super().__iter__()


def test_deferred_messages_go_back_without_a_walk_of_the_history_for_each() -> None:
    """A rebuilt window defers every later note; each used to be placed by its own walk."""
    whole = _conversation(60)
    deferred_indices = [index for index in range(1, len(whole) - 1) if index % 2]
    deferred = [(whole[index], whole[index + 1]) for index in deferred_indices]
    kept = _Walked(message for index, message in enumerate(whole) if not index % 2)
    view = ReplayView(kept, deferred=deferred)

    view.restore_later()

    assert len(deferred) > 50, "the premise: many messages to put back"
    assert kept.walks < len(deferred) / 4
    assert all(now is then for now, then in zip(view.messages, whole, strict=True))


def _deferred_case(seed: int) -> ReplayView:
    """Return a view with deferred notes, summaries and staged outputs, some hard to place."""
    rng = random.Random(seed)
    whole: list[Message] = []
    for index in range(rng.randint(8, 40)):
        identity = rng.choice([f"m{index}", f"m{index}", f"m{rng.randrange(40)}", None])
        message = Message(rng.choice(["user", "assistant"]), [f"Text {index}."])
        message.message_id = identity
        whole.append(message)
    known = [message.message_id for message in whole if message.message_id] + ["missing"]
    chosen = sorted(rng.sample(range(len(whole)), rng.randint(1, len(whole) // 2)))
    deferred: list[tuple[Message, Message | None]] = []
    outputs: set[int] = set()
    standing: list[Message] = []
    for index in chosen:
        message = whole[index]
        following = whole[index + 1] if index + 1 < len(whole) else None
        kind = rng.choice(["plain", "linked", "output", "regenerated"])
        if kind == "linked":
            message.additional_properties[GROUP_ANNOTATION_KEY] = {
                SUMMARY_OF_MESSAGE_IDS_KEY: rng.sample(known, rng.randint(0, 3))
            }
        elif kind in {"output", "regenerated"}:
            message.additional_properties[LINEAGE_KEY] = f"lineage{index % 3}"
            message.additional_properties[EXCLUDED_KEY] = rng.choice([True, False])
            outputs.add(id(message))
            if kind == "regenerated":
                again = Message("assistant", [f"Text {index}."])
                again.additional_properties[LINEAGE_KEY] = f"lineage{index % 3}"
                again.additional_properties["regenerated"] = index
                standing.append(again)
        # A following message replaced by a copy is found by its id, as after an exclusion.
        if following is not None and rng.random() < 0.3:
            position = whole.index(following)
            whole[position] = copy.copy(following)
        deferred.append((message, following))
    staged = {id(message) for message, _ in deferred}
    messages = [message for message in whole if id(message) not in staged]
    for message in standing:
        messages.insert(rng.randint(0, len(messages)), message)
    return ReplayView(messages, deferred=deferred, record_replacements=outputs)


def _places(view: ReplayView, place: Any) -> list[tuple[Any, ...]]:
    """Run ``place`` and describe the history it leaves by where each message came from."""
    # Held to the end: a message that leaves the history must not lend its id to a new one.
    given = [*view.messages, *(message for message, _ in view.deferred)]
    origin = {id(message): index for index, message in enumerate(given)}
    place(view)
    return [
        (
            origin.get(id(message), "restored"),
            message.message_id,
            message.text,
            json.dumps(message.additional_properties, sort_keys=True, default=str),
        )
        for message in view.messages
    ]


@pytest.mark.parametrize("seed", range(120))
def test_placing_deferred_messages_agrees_with_a_walk_for_each(seed: int) -> None:
    """Same order, same objects, and a staged output restored over its regenerated form."""
    walked = _places(_deferred_case(seed), ReplayView._scan_for_deferred)
    indexed = _places(_deferred_case(seed), lambda view: view._place_deferred() or pytest.fail())

    assert indexed == walked


@pytest.mark.parametrize("oddity", ["numeric-id", "numeric-source", "sources-not-a-list"])
def test_an_id_the_index_cannot_hold_keeps_the_walk(oddity: str) -> None:
    whole = _conversation(4)
    note = whole[3]
    kept = [message for message in whole if message is not note]
    if oddity == "numeric-id":
        kept[1].message_id = 7  # type: ignore[assignment]
    elif oddity == "numeric-source":
        note.additional_properties[GROUP_ANNOTATION_KEY][SUMMARY_OF_MESSAGE_IDS_KEY] = [7]
    else:
        note.additional_properties[GROUP_ANNOTATION_KEY][SUMMARY_OF_MESSAGE_IDS_KEY] = ("ask0",)
    view = ReplayView(list(kept), deferred=[(note, whole[4])])

    assert not view._place_deferred()
    assert all(now is then for now, then in zip(view.messages, kept, strict=True))

    view.restore_later()
    assert note in view.messages


def test_shed_group_keys_are_the_ones_a_fresh_reading_gives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copies share their contents, so a group of copies is read from the first reading."""
    messages = _conversation(6)
    expected = unique_shed_groups(messages)
    keys = ShedGroupKeys()
    calls: Counter[str] = Counter()
    _counting(monkeypatch, shed_module, "_group_key", calls)

    first = unique_shed_groups(messages, keys=keys)
    read = calls["_group_key"]
    clones = [copy.copy(message) for message in messages]
    second = unique_shed_groups(clones, keys=keys)

    assert [key for key, _ in first] == [key for key, _ in expected] == [key for key, _ in second]
    assert read == len(messages) and calls["_group_key"] == read


@pytest.mark.parametrize("change", ["replaced", "appended", "role", "author"])
def test_shed_group_keys_reread_what_changed(change: str) -> None:
    message = Message("assistant", ["First.", "Second."], message_id="reply")
    keys = ShedGroupKeys()
    before = keys.key([message])
    held = list(message.contents)
    if change == "replaced":
        message.contents[1] = Content.from_text("Another.")
    elif change == "appended":
        message.contents.append(Content.from_text("Third."))
    elif change == "role":
        message.role = "user"
    else:
        message.author_name = "another"

    assert keys.key([message]) == shed_module._group_key([message]) != before
    assert held


@pytest.mark.parametrize("field", ["role", "author_name"])
def test_shed_group_keys_read_afresh_what_they_cannot_key(field: str) -> None:
    """``1`` and ``True`` are one dictionary key and two serializations; neither is kept."""
    keys = ShedGroupKeys()
    contents: list[Any] = ["A body."]
    readings: list[str] = []
    for value in (1, True):
        message = Message("assistant", contents, message_id="reply")
        message.contents = contents = message.contents
        setattr(message, field, value)
        assert keys.key([message]) == shed_module._group_key([message])
        readings.append(keys.key([message]))

    assert readings[0] != readings[1]
    assert not keys.groups


def _media_history() -> list[Message]:
    messages = _conversation(5)
    messages[4].contents.append(
        Content.from_uri("https://example.test/a.png", media_type="image/png")
    )
    return messages


@pytest.mark.parametrize("media", [False, True])
@pytest.mark.parametrize("from_index", [0, 3, 7, 40])
def test_shared_token_counts_are_the_ones_the_framework_takes(media: bool, from_index: int) -> None:
    """Counting from an index through the shared counts annotates exactly as without them."""
    plain = _media_history() if media else _conversation(5)
    shared = _media_history() if media else _conversation(5)
    for messages in (plain, shared):
        annotate_token_counts(messages, tokenizer=TOKENIZER)
        messages.insert(3, Message("assistant", ["A record put back."], message_id="record"))
    counts = TokenCounts()

    annotate_token_counts(plain, tokenizer=TOKENIZER, from_index=from_index)
    for _ in range(2):
        annotate_token_counts(shared, tokenizer=TOKENIZER, from_index=from_index, counts=counts)

    assert [message.to_dict() for message in plain] == [message.to_dict() for message in shared]
    assert counts.entries


def test_shared_token_counts_are_taken_once_for_a_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy shares its contents with the message already counted, and is not counted again."""
    messages = _conversation(20)
    annotate_token_counts(messages, tokenizer=TOKENIZER)
    counts = TokenCounts()
    calls: Counter[str] = Counter()
    _counting(monkeypatch, type(TOKENIZER), "count_tokens", calls)

    annotate_token_counts(messages, tokenizer=TOKENIZER, from_index=1, counts=counts)
    first = calls["count_tokens"]
    clones = [copy.copy(message) for message in messages]
    for clone in clones:
        clone.additional_properties = copy.deepcopy(clone.additional_properties)
    annotate_token_counts(clones, tokenizer=TOKENIZER, from_index=1, counts=counts)

    assert first == len(messages) - 1 and calls["count_tokens"] == first
    probe = copy.deepcopy(messages)
    framework_annotate_token_counts(probe, tokenizer=TOKENIZER, force_retokenize=True)
    assert [m.to_dict() for m in clones[1:]] == [m.to_dict() for m in probe[1:]]


def test_shared_token_counts_reread_contents_that_changed() -> None:
    messages = _conversation(3)
    annotate_token_counts(messages, tokenizer=TOKENIZER)
    counts = TokenCounts()
    annotate_token_counts(messages, tokenizer=TOKENIZER, from_index=0, counts=counts)
    messages[2].contents.append(Content.from_text("More text than there was before."))

    annotate_token_counts(messages, tokenizer=TOKENIZER, from_index=0, counts=counts)

    fresh = copy.deepcopy(messages)
    framework_annotate_token_counts(fresh, tokenizer=TOKENIZER, force_retokenize=True)
    assert [m.to_dict() for m in messages] == [m.to_dict() for m in fresh]
