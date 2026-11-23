"""Recognize compaction text and distinguish its stored provenance from model output."""

from typing import Final

from agent_framework import Message
from agent_framework._compaction import EXCLUDED_KEY, set_excluded

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
ANSWERED_NOTE: Final[str] = (
    "[compacted: the preceding user request was already answered; the assistant reply was removed]"
)

_NOTE_TEXTS = frozenset((*SHED_NOTES.values(), ANSWERED_NOTE))

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


def is_marker(message: Message) -> bool:
    """Return whether ``message`` is a note this strategy left in place of a dropped group.

    Source links distinguish generated notes from ordinary messages with matching text and ids.
    """
    return bool(
        message.role == "assistant"
        and has_summary_links(message)
        and message.message_id
        and message.message_id.startswith(MARKER_ID_PREFIX)
        and message.text in _NOTE_TEXTS
    )


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
        return output in _NOTE_TEXTS or output.startswith(RECORD_MARKER)
    if output.role != "assistant" or any(c.type != "text" for c in output.contents):
        return False
    text = output.text
    if text in _NOTE_TEXTS:
        return not is_marker(output)
    return text.startswith(RECORD_MARKER) and not is_written_record(output)


def exclude_compaction_echoes(messages: list[Message]) -> bool:
    """Exclude echoed replies from the prompt while retaining their source in history."""
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
    return changed
