"""The persisted view's parts, their fit check, the turn boundary and the store's write fence."""

from __future__ import annotations

import json

import pytest
from agent_framework import Content, Message
from agent_framework._compaction import GROUP_ANNOTATION_KEY, SUMMARY_OF_MESSAGE_IDS_KEY

from maf_compaction._carryover import Handed
from maf_compaction._persisted_view import (
    VIEW_PART_VERSION,
    InMemoryViewStore,
    MisfitReason,
    PersistedViewPart,
    ViewKey,
    ViewPart,
    ViewWriteConflict,
    split_saved_layout,
    turn_boundary,
)
from maf_compaction._saved_view import _flags, _state
from maf_compaction._view_digest import canonical_json, json_digest

pytestmark = pytest.mark.anyio


def _history() -> list[Message]:
    return [
        Message("user", ["first"], message_id="u1"),
        Message("assistant", [Content.from_text("answer one")], message_id="a1"),
        Message("user", ["second"], message_id="u2"),
        Message("assistant", [Content.from_text("answer two")], message_id="a2"),
    ]


def _layout(messages: list[Message], order: list[dict], required: int = 0) -> str:
    """A layout recording each message as save_view does: digest, state, flags and links."""
    handed = Handed.take(messages)
    sources = [
        [
            handed.digests[i],
            [_state(handed.states[i])],
            [list(_flags(m.additional_properties))],
            [json_digest(handed.links[i])],
        ]
        for i, m in enumerate(messages)
    ]
    return canonical_json({"sources": sources, "order": order, "required": required})


def _part(messages: list[Message], start: int, end: int, config: str = "cfg") -> PersistedViewPart:
    layout = _layout(messages[start:end], [{"source": i} for i in range(end - start)])
    return PersistedViewPart(ViewPart.CURRENT, start, end, layout, config)


def test_a_part_round_trips_and_refuses_another_version() -> None:
    part = _part(_history(), 2, 4)

    assert PersistedViewPart.from_dict(json.loads(json.dumps(part.to_dict()))) == part
    assert PersistedViewPart.from_dict({**part.to_dict(), "version": VIEW_PART_VERSION + 1}) is None
    assert PersistedViewPart.from_dict({**part.to_dict(), "layout": "{not json"}) is None


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("none", None),
        ("config", MisfitReason.CONFIG),
        ("shorter", MisfitReason.RANGE),
        ("edited", MisfitReason.DIGEST),
    ],
)
def test_a_part_says_why_it_no_longer_fits(change: str, reason: MisfitReason | None) -> None:
    messages = _history()
    part = _part(messages, 2, 4)
    config = "cfg"
    if change == "config":
        config = "other"
    elif change == "shorter":
        messages = messages[:3]
    elif change == "edited":
        messages[3] = Message("assistant", [Content.from_text("answer changed")], message_id="a2")

    assert part.misfit(messages, config) is reason


def test_the_current_turn_starts_at_the_last_user_message() -> None:
    assert turn_boundary(_history()) == 2
    assert turn_boundary([Message("assistant", ["hi"])]) == 0


def test_a_layout_splits_at_the_boundary_and_inserts_stay_with_their_part() -> None:
    record = {"insert": {"role": "assistant", "contents": [{"type": "text", "text": "record"}]}}
    order = [record, {"source": 0}, {"source": 1}, record, {"source": 2}, {"source": 3}, record]
    layout = _layout(_history(), order, required=3)

    earlier, current = split_saved_layout(layout, 2) or ("", "")

    earlier_data, current_data = json.loads(earlier), json.loads(current)
    assert len(earlier_data["sources"]) == 2 and len(current_data["sources"]) == 2
    assert earlier_data["order"] == [record, {"source": 0}, {"source": 1}, record]
    assert current_data["order"] == [{"source": 0}, {"source": 1}, record]
    assert (earlier_data["required"], current_data["required"]) == (2, 1)
    assert split_saved_layout(layout, 5) is None
    assert split_saved_layout("{", 1) is None


async def test_a_stale_writer_is_refused_and_parts_are_kept_apart() -> None:
    store = InMemoryViewStore()
    advisor = ViewKey("conversation", "advisor", ViewPart.EARLIER)
    child = ViewKey("conversation", "reviewer", ViewPart.EARLIER)

    first = await store.write(advisor, {"n": 1}, etag=None)
    second = await store.write(advisor, {"n": 2}, etag=first)
    with pytest.raises(ViewWriteConflict):
        await store.write(advisor, {"n": 3}, etag=first)
    with pytest.raises(ViewWriteConflict):
        await store.write(advisor, {"n": 3}, etag=None)
    await store.write(child, {"n": 9}, etag=None)

    stored = await store.read(advisor)
    assert stored is not None and stored.data == {"n": 2} and stored.etag == second
    child_stored = await store.read(child)
    assert child_stored is not None and child_stored.data == {"n": 9}
    await store.delete(advisor)
    await store.delete(advisor)
    assert await store.read(advisor) is None


@pytest.mark.parametrize(
    "layout",
    [
        '{"sources": null, "order": [], "required": 0}',
        '{"sources": [[]], "order": [], "required": 0}',
        '{"sources": [["d", [], [], []]], "order": [{"source": 3}], "required": 0}',
        '{"sources": [], "order": [7], "required": 0}',
        '{"sources": [], "order": [], "required": -1}',
        "[]",
    ],
)
def test_a_malformed_layout_is_rejected_everywhere_without_raising(layout: str) -> None:
    part = PersistedViewPart(ViewPart.CURRENT, 0, 1, layout, "cfg")

    assert PersistedViewPart.from_dict(part.to_dict()) is None
    assert part.misfit(_history(), "cfg") is MisfitReason.LAYOUT
    assert split_saved_layout(layout, 0) is None


@pytest.mark.parametrize("change", ["id", "role"])
def test_the_same_text_under_another_identity_does_not_fit(change: str) -> None:
    messages = _history()
    part = _part(messages, 2, 4)
    replaced = messages[3]
    if change == "id":
        messages[3] = Message("assistant", replaced.contents, message_id="other-branch")
    else:
        messages[3] = Message("user", replaced.contents, message_id=replaced.message_id)

    assert part.misfit(messages, "cfg") is MisfitReason.IDENTITY


def _summary(*ids: str) -> dict:
    return {
        "role": "user",
        "contents": [{"type": "text", "text": "summary"}],
        "additional_properties": {GROUP_ANNOTATION_KEY: {SUMMARY_OF_MESSAGE_IDS_KEY: list(ids)}},
    }


def test_a_summary_inserted_before_its_sources_goes_with_the_part_holding_them() -> None:
    messages = _history()
    sources = [{"source": i, "id": m.message_id} for i, m in enumerate(messages)]
    order = [sources[0], sources[1], {"insert": _summary("u2", "a2")}, sources[2], sources[3]]

    earlier, current = split_saved_layout(_layout(messages, order), 2) or ("", "")

    assert {"insert": _summary("u2", "a2")} not in json.loads(earlier)["order"]
    assert json.loads(current)["order"][0] == {"insert": _summary("u2", "a2")}


def test_a_summary_standing_for_sources_on_both_sides_is_refused() -> None:
    messages = _history()
    sources = [{"source": i, "id": m.message_id} for i, m in enumerate(messages)]
    order = [sources[0], {"insert": _summary("a1", "u2")}, *sources[1:]]

    assert split_saved_layout(_layout(messages, order), 2) is None
