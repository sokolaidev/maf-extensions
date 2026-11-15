"""Resume a per-call pass from the list the previous call's pass left.

A host that compacts before every model call rarely hands the strategy the list it compacted
last time. The framework's chat-middleware layer compacts a copy of the tool loop's list, and
per-service-call history persistence rebuilds each call's list as fresh copies of the stored
messages. Either way the records, notes and exclusions a pass left are gone on the next call,
and putting every decision back costs as much as a long conversation holds.

What does survive is each stored message's ``contents`` list: a rebuilt list holds shallow
copies, and a shared one holds the messages themselves. So a pass keeps the list it left
together with what it was handed, and the next call, handed the same messages in the same
order and state plus new ones at the end, starts from the kept list and those new messages.
Anything else is a different conversation as far as this module can tell, and the pass runs as
it always has.

The kept list is a cache local to the process and to one strategy instance. It is never part of
a session snapshot, and losing it costs one pass that puts its decisions back the slow way.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final, cast

from agent_framework import Content, Message
from agent_framework._compaction import (
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    GROUP_HAS_REASONING_KEY,
    GROUP_ID_KEY,
    GROUP_INDEX_KEY,
    GROUP_KIND_KEY,
    GROUP_TOKEN_COUNT_BASIS_KEY,
    GROUP_TOKEN_COUNT_KEY,
)

from ._preserve import _ATTRIBUTION_KEY  # pyright: ignore[reportPrivateUsage]
from ._shed import durable_message_id

logger = logging.getLogger(__name__)

#: Written anew on every list a call is handed, so no evidence that a stored message changed.
_PER_CALL_PROPERTIES: Final[frozenset[str]] = frozenset({GROUP_ANNOTATION_KEY, _ATTRIBUTION_KEY})
_PER_CALL_ANNOTATIONS: Final[frozenset[str]] = frozenset(
    {
        GROUP_ID_KEY,
        GROUP_KIND_KEY,
        GROUP_INDEX_KEY,
        GROUP_HAS_REASONING_KEY,
        GROUP_TOKEN_COUNT_KEY,
        GROUP_TOKEN_COUNT_BASIS_KEY,
    }
)


def _frozen(value: Any) -> Any:
    """Return a value that later in-place edits of ``value`` cannot change."""
    if isinstance(value, dict):
        mapping = cast(dict[Any, Any], value)
        pairs = [(str(key), _frozen(item)) for key, item in mapping.items()]
        return tuple(sorted(pairs, key=lambda pair: pair[0]))
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(_frozen(item) for item in cast(Iterable[Any], value))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _annotation(message: Message) -> dict[str, Any] | None:
    annotation = message.additional_properties.get(GROUP_ANNOTATION_KEY)
    return cast(dict[str, Any], annotation) if isinstance(annotation, dict) else None


def _host_state(message: Message) -> tuple[Any, ...]:
    """Return what a host can change on a message between calls, besides its contents.

    A positional ``msg_N`` id is assigned afresh to each list and is no part of it. Neither is
    the group annotation: see :func:`_links`.
    """
    properties = message.additional_properties
    return (
        message.role,
        message.author_name,
        message.message_id if durable_message_id(message.message_id) else None,
        _frozen({key: item for key, item in properties.items() if key not in _PER_CALL_PROPERTIES}),
    )


def _links(message: Message) -> Any:
    """Return the summary links in a message's group annotation.

    The framework hands a loaded message as a shallow copy, so the copy's own flags stay on it
    while the annotation it shares with the stored message does not: a link a pass writes on a
    copy can show on the store. Links are therefore no evidence of a host's change, and are
    compared only to tell whether two messages are interchangeable.
    """
    annotation = _annotation(message)
    if annotation is None:
        return ()
    return _frozen(
        {key: item for key, item in annotation.items() if key not in _PER_CALL_ANNOTATIONS}
    )


def _token_count(message: Message) -> int | None:
    annotation = _annotation(message)
    count = None if annotation is None else annotation.get(GROUP_TOKEN_COUNT_KEY)
    return count if isinstance(count, int) else None


def _drop_token_count(message: Message) -> None:
    annotation = _annotation(message)
    if annotation is not None:
        annotation.pop(GROUP_TOKEN_COUNT_KEY, None)


def _sent(messages: list[Message]) -> list[int]:
    """Identify what a list sends: the contents of its included messages, in order."""
    return [
        id(message.contents)
        for message in messages
        if not message.additional_properties.get(EXCLUDED_KEY, False)
    ]


@dataclass
class Handed:
    """A list as a call handed it, before its pass changed anything."""

    messages: list[Message]
    contents: list[list[Content]]
    states: list[tuple[Any, ...]]
    links: list[Any]

    @classmethod
    def take(cls, messages: list[Message]) -> Handed:
        return cls(
            list(messages),
            [message.contents for message in messages],
            [_host_state(message) for message in messages],
            [_links(message) for message in messages],
        )


@dataclass(frozen=True)
class Resumed:
    """How a later call's list took up a kept view."""

    #: The view replaced the stored messages; False when the list already began with it.
    restored: bool
    #: The list now sends something else than the call handed.
    changed: bool


class CarriedView:
    """The list a pass left, and the list that pass was handed."""

    def __init__(self, handed: Handed, view: list[Message]) -> None:
        self.view = view
        self._contents = handed.contents
        self._states = handed.states
        self._members = {id(message) for message in view}
        # The message standing in the view for each one handed: itself, or the shallow copy a
        # pass put in its place. The package never rebinds a contents list, so both share it.
        by_contents = {id(message.contents): message for message in view}
        self._standing = [by_contents.get(id(contents)) for contents in handed.contents]
        # A handed message the pass replaced is not in the view, so nothing shows what a host
        # does to it afterwards. The state the pass left it in is kept to tell the two apart.
        self._replaced = {
            index: _host_state(message)
            for index, message in enumerate(handed.messages)
            if id(message) not in self._members
        }

    def _unchanged(self, position: int, state: tuple[Any, ...]) -> bool:
        """Return whether a stored message is in a state this view accounts for."""
        if state == self._states[position]:
            return True
        standing = self._standing[position]
        # The host stores the very message the view holds, changed by a pass or by the host.
        if standing is not None and state == _host_state(standing):
            return True
        return state == self._replaced.get(position)

    def resume(self, messages: list[Message], current: Handed) -> Resumed | None:
        """Replace the messages this view was built from with the view, keeping what is new.

        The list a call is handed says which messages are loaded copies and which the call
        carried in, by the framework's attribution mark, and the package reads that mark to
        know where an exclusion flag lasts (see ``removable_whole``). So the view is brought
        to what this call was handed before it stands in: where the store holds what the view
        holds, this call's own message takes the view's place, and every other message of the
        view takes this call's mark. A pass that resumes then sets its flags where a pass over
        the handed list would have.

        Args:
            messages: The list a later call was handed, mutated in place when it resumes.
            current: That list as handed.

        Returns:
            None when the list does not continue the one this view was built from, in which
            case nothing was changed.
        """
        view = self.view
        if len(messages) >= len(view) and all(
            message is kept for message, kept in zip(messages, view, strict=False)
        ):
            # A host that hands back the very list the pass left has nothing to restore.
            return Resumed(restored=False, changed=False)
        members = self._members
        stored = len(self._contents)
        position = 0
        tail_start = len(messages)
        paired: list[tuple[Message, Message, int]] = []
        for index, message in enumerate(messages):
            if position < stored and message.contents is self._contents[position]:
                if not self._unchanged(position, current.states[index]):
                    logger.debug(
                        "Compaction put its decisions back in full: stored message %d of %d "
                        "changed between calls.",
                        position,
                        stored,
                    )
                    return None
                standing = self._standing[position]
                if standing is not None:
                    paired.append((standing, message, index))
                position += 1
            elif id(message) in members:
                # The host kept something the pass inserted, and the view already holds it.
                continue
            elif position == stored:
                tail_start = index
                break
            else:
                logger.debug(
                    "Compaction put its decisions back in full: the list departs from the last "
                    "call's at stored message %d of %d.",
                    position,
                    stored,
                )
                return None
        if position < stored:
            logger.debug(
                "Compaction put its decisions back in full: %d of the last call's %d messages "
                "were handed.",
                position,
                stored,
            )
            return None
        tail = [message for message in messages[tail_start:] if id(message) not in members]
        before = _sent(messages)
        handed_now: dict[int, Message] = {}
        for standing, message, index in paired:
            if standing is message:
                continue
            if current.states[index] == _host_state(standing) and current.links[index] == _links(
                standing
            ):
                # A positional id is this list's own; the view's is the one its notes refer to.
                message.message_id = standing.message_id
                handed_now[id(standing)] = message
                continue
            if _ATTRIBUTION_KEY in message.additional_properties:
                standing.additional_properties[_ATTRIBUTION_KEY] = message.additional_properties[
                    _ATTRIBUTION_KEY
                ]
            else:
                standing.additional_properties.pop(_ATTRIBUTION_KEY, None)
            # A count the host dropped is one it wants taken again.
            if _token_count(message) is None:
                _drop_token_count(standing)
        resumed = [handed_now.get(id(message), message) for message in view]
        taken = {message.message_id for message in resumed if message.message_id}
        for message in tail:
            # This list's positional ids were assigned without the view's, so they can collide.
            if message.message_id in taken and not durable_message_id(message.message_id):
                message.message_id = None
        messages[:] = [*resumed, *tail]
        return Resumed(restored=True, changed=_sent(messages) != before)
