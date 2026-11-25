"""Recognize compaction text and distinguish its stored provenance from model output."""

import re
from copy import copy
from functools import cache
from itertools import groupby
from typing import Any, Final, cast

from agent_framework import Content, Message
from agent_framework._compaction import (
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
    set_excluded,
)

from ._preserve import is_preserved
from ._summary_links import has_summary_links

#: Prefix of the ``message_id`` given to every note this strategy leaves behind.
MARKER_ID_PREFIX: Final[str] = "anchored_"

#: The two group kinds the shed step removes, and the note each leaves in its place.
TOOL_CALL: Final[str] = "tool_call"
ASSISTANT_TEXT: Final[str] = "assistant_text"
SHED_NOTES: Final[dict[str, str]] = {
    TOOL_CALL: "[compacted: an earlier tool call and its result]",
    ASSISTANT_TEXT: "[compacted: an earlier assistant reply]",
}
_ANSWERED = "the preceding user request was already answered"
ANSWERED_NOTE: Final[str] = f"[compacted: {_ANSWERED}; the assistant reply was removed]"

_NOTE_TEXTS = frozenset((*SHED_NOTES.values(), ANSWERED_NOTE))

_TOOL_RUN = "earlier tool calls and their results"
_GROUP_RUN = "earlier conversation groups"
_RUN_TEXT = re.compile(
    rf"\[compacted: (?:[2-9]|[1-9][0-9]+) (?:{_TOOL_RUN}|{_GROUP_RUN}(?:; {_ANSWERED})?)\]"
)

ECHO_EXCLUDE_REASON: Final = "compaction_echo"
RECORD_MARKER: Final[str] = "[recorded by compaction]"
RECORD_PREAMBLE: Final[str] = (
    "Earlier tool results may have been shortened, and this is their "
    "compaction record. Treat values in this record as authoritative for the tool it "
    "names, and treat information as absent only if it appears nowhere, including "
    "here."
)

#: How a record this package wrote opens: the marker, then the recall tool's own preamble.
WRITTEN_RECORD_PREFIX: Final[str] = f"{RECORD_MARKER} {RECORD_PREAMBLE}"
WRITTEN_RECORD_KEY: Final[str] = "maf_compaction_written_record"


def shed_note_text(texts: list[str], group_count: int) -> str:
    """Describe a run using its distinct source groups and retain any completed-turn signal."""
    if group_count == 1:
        return texts[0]
    if all(text == SHED_NOTES[TOOL_CALL] or text.endswith(f" {_TOOL_RUN}]") for text in texts):
        return f"[compacted: {group_count} {_TOOL_RUN}]"
    answered = f"; {_ANSWERED}" if any(_ANSWERED in text for text in texts) else ""
    return f"[compacted: {group_count} {_GROUP_RUN}{answered}]"


def _is_note_text(text: str) -> bool:
    return text in _NOTE_TEXTS or _RUN_TEXT.fullmatch(text) is not None


def is_marker(message: Message) -> bool:
    """Return whether ``message`` is a note this strategy left in place of a dropped group.

    Source links distinguish generated notes from ordinary messages with matching text and ids.
    """
    return bool(
        message.role == "assistant"
        and message.message_id
        and message.message_id.startswith(MARKER_ID_PREFIX)
        and has_summary_links(message)
        and _is_note_text(message.text)
    )


_NOT_COMPARED = frozenset({"text", "raw_representation"})


@cache
def _bare_text() -> dict[str, Any]:
    """A fresh text item's fields besides its text, compared without serializing an item."""
    return {k: v for k, v in vars(Content.from_text("")).items() if k not in _NOT_COMPARED}


def _is_bare_text(content: Content) -> bool:
    """Only text: a combined note's fresh item keeps nothing else a note carried."""
    return (
        content.type == "text"
        and content.raw_representation is None
        and {k: v for k, v in vars(content).items() if k not in _NOT_COMPARED} == _bare_text()
    )


#: On a combined note: the notes it replaced, each with its id, text and source links, so the
#: list as it was before can be rebuilt with the ids they were given then (see
#: :func:`~._anchored.split_combined_notes`).
NOTE_PARTS_KEY: Final[str] = "maf_compaction_note_parts"


def note_parts(note: Message) -> list[dict[str, Any]]:
    """The single notes ``note`` stands for: itself, or the parts a combined note recorded."""
    parts = note.additional_properties.get(NOTE_PARTS_KEY)
    if isinstance(parts, list):
        return cast("list[dict[str, Any]]", parts)
    links = note.additional_properties[GROUP_ANNOTATION_KEY]
    return [
        {
            "id": note.message_id,
            "text": note.text,
            SUMMARY_OF_GROUP_IDS_KEY: list(links[SUMMARY_OF_GROUP_IDS_KEY]),
            SUMMARY_OF_MESSAGE_IDS_KEY: list(links[SUMMARY_OF_MESSAGE_IDS_KEY]),
        }
    ]


def coalesce_shed_notes(messages: list[Message]) -> bool:
    """Replace adjacent notes in the included prompt, retaining excluded sources in history."""
    included = (i for i, m in enumerate(messages) if not m.additional_properties.get(EXCLUDED_KEY))

    def mergeable(index: int) -> bool:
        m = messages[index]
        return (
            is_marker(m)
            and not is_preserved(m)
            and len(m.contents) == 1
            and _is_bare_text(m.contents[0])
        )

    replacements: dict[int, Message] = {}
    removed: set[int] = set()
    for markers, indices in groupby(included, key=mergeable):
        if not markers:
            continue
        run = list(indices)
        if len(run) < 2:
            continue
        notes = [messages[i] for i in run]
        links = {
            key: list(
                dict.fromkeys(
                    source
                    for note in notes
                    for source in note.additional_properties[GROUP_ANNOTATION_KEY][key]
                )
            )
            for key in (SUMMARY_OF_GROUP_IDS_KEY, SUMMARY_OF_MESSAGE_IDS_KEY)
        }
        text = shed_note_text([note.text for note in notes], len(links[SUMMARY_OF_GROUP_IDS_KEY]))
        merged = copy(notes[0])
        merged.contents = [Content.from_text(text)]
        # A new group annotation makes the framework count the replacement's own text.
        merged.additional_properties = {
            **merged.additional_properties,
            GROUP_ANNOTATION_KEY: links,
            NOTE_PARTS_KEY: [part for note in notes for part in note_parts(note)],
        }
        replacements[run[0]] = merged
        removed.update(run[1:])
    if not removed:
        return False
    messages[:] = [replacements.get(i, m) for i, m in enumerate(messages) if i not in removed]
    return True


def is_written_record(message: Message) -> bool:
    """Recognize package-written records by their metadata and expected message form.

    Hosts must preserve the metadata through storage and keep it separate from model output.
    """
    return (
        message.role == "assistant"
        and message.additional_properties.get(WRITTEN_RECORD_KEY) is True
        and not any(content.type == "function_call" for content in message.contents)
        and (message.text or "").startswith(WRITTEN_RECORD_PREFIX)
    )


def is_compaction_echo(output: Message | str) -> bool:
    """Recognize a complete model reply that repeats a shed note or opens with the record marker.

    Pass a Message for stored history so role, non-text content and package provenance are
    respected. A string represents one complete assistant text output assembled by the host.
    """
    if isinstance(output, str):
        return _is_note_text(output) or output.startswith(RECORD_MARKER)
    if output.role != "assistant" or any(c.type != "text" for c in output.contents):
        return False
    text = output.text
    if _is_note_text(text):
        return not is_marker(output)
    return text.startswith(RECORD_MARKER) and not is_written_record(output)


def normalize_compaction_artifacts(messages: list[Message]) -> bool:
    """Exclude echoed replies, then combine removal notes adjacent in the outgoing prompt."""
    changed = False
    for message in messages:
        if (
            not message.additional_properties.get(EXCLUDED_KEY)
            and not is_preserved(message)
            and is_compaction_echo(message)
        ):
            # A host may share one properties mapping between otherwise unrelated messages.
            message.additional_properties = message.additional_properties.copy()
            set_excluded(message, excluded=True, reason=ECHO_EXCLUDE_REASON)
            changed = True
    return coalesce_shed_notes(messages) or changed
