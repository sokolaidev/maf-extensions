"""Resume a per-call pass from the list the previous call's pass left.

A host that compacts before every model call rarely hands the strategy the list it compacted
last time. The framework's chat-middleware layer compacts a copy of the tool loop's list, and
per-service-call history persistence rebuilds each call's list as fresh copies of the stored
messages. Either way the records, notes and exclusions a pass left are gone on the next call,
and putting every decision back costs as much as a long conversation holds.

What does survive is each stored message's contents: a rebuilt list holds shallow copies, and a
shared one holds the messages themselves. So a pass keeps the list it left together with what
it was handed, and the next call, handed the same messages in the same order and state plus
new ones at the end, starts from the kept list and those new messages. Anything else is a
different conversation as far as this module can tell, and the pass runs as it always has.

The live list belongs to one strategy instance. A turn can save its layout separately from
the session's decisions; losing either form of the cache costs one full replay.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from copy import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

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
    SUMMARIZED_BY_SUMMARY_ID_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
    group_messages,
)

from ._anchored import AnchoredCompactionStrategy, _is_marker  # pyright: ignore[reportPrivateUsage]
from ._media import MESSAGE_COUNTED
from ._preserve import _ATTRIBUTION_KEY, removable_spans  # pyright: ignore[reportPrivateUsage]
from ._request import RECALL_PROMPT_KEY
from ._shed import durable_message_id
from ._view_digest import content_digest

if TYPE_CHECKING:
    from ._tool_rounds import SettledTools

logger = logging.getLogger(__name__)

#: Written anew on every list a call is handed, so no evidence that a stored message changed.
_PER_CALL_PROPERTIES: Final[frozenset[str]] = frozenset(
    {GROUP_ANNOTATION_KEY, _ATTRIBUTION_KEY, MESSAGE_COUNTED}
)
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
    the group annotation: see :func:`_links`. A pass writes an included message's flag out as
    false, which says no more than its absence.
    """
    properties = {
        key: item
        for key, item in message.additional_properties.items()
        if key not in _PER_CALL_PROPERTIES and (key != EXCLUDED_KEY or item)
    }
    return (
        message.role,
        message.author_name,
        message.message_id if durable_message_id(message.message_id) else None,
        _frozen(properties),
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


def _same_contents(handed: Sequence[Content], stored: Sequence[Content]) -> bool:
    """Return whether a handed message carries the contents a stored one was handed with.

    Usually the very items: a loaded copy shares its list. A host can hand a stored message under
    a new list on every load -- the framework rebuilds one that mixes a resolved approval with
    other content -- and then the items are the same objects, or equal ones the host wrote in.
    """
    if handed is stored:
        return True
    return len(handed) == len(stored) and all(
        new is old or (new.type == old.type and new.to_dict() == old.to_dict())
        for new, old in zip(handed, stored, strict=True)
    )


def _is_transient(message: Message) -> bool:
    """Return whether a message is the recall prompt, which is handed for one call only."""
    return bool(message.additional_properties.get(RECALL_PROMPT_KEY))


def _sent(messages: list[Message], handed: dict[int, Message] | None = None) -> list[int]:
    """Identify what a list sends: its included messages, in order.

    Args:
        messages: The list.
        handed: For a message standing in for a handed one, the handed one, by identity.
    """
    stands_for = handed or {}
    return [
        id(stands_for.get(id(message), message))
        for message in messages
        if not message.additional_properties.get(EXCLUDED_KEY, False)
    ]


@dataclass
class Handed:
    """A list as a call handed it, before its pass changed anything."""

    messages: list[Message]
    #: Each message's items as handed: a list edited in place afterwards no longer matches.
    contents: list[tuple[Content, ...]]
    digests: list[str | None]
    states: list[tuple[Any, ...]]
    links: list[Any]
    #: The message put in the list for each one handed: itself, unless a kept one stood in.
    stood: list[Message]
    portable: bool = True

    @classmethod
    def take(cls, messages: list[Message]) -> Handed:
        return cls(
            list(messages),
            [tuple(message.contents) for message in messages],
            [content_digest(message.contents) for message in messages],
            [_host_state(message) for message in messages],
            [_links(message) for message in messages],
            list(messages),
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

    def __init__(
        self, handed: Handed, view: list[Message], *, continuing: CarriedView | None = None
    ) -> None:
        """Keep ``view`` for the next call.

        Args:
            handed: The list as the call handed it.
            view: The list as the pass left it.
            continuing: The view the handed list already began with. A list handed back as the
                last pass left it holds that pass's records and notes, which no rebuilt list
                will, so what was handed before it stays the account of the stored messages.
        """
        self.view = [message for message in view if not _is_transient(message)]
        self.settled_tools: SettledTools | None = None
        self.portable = handed.portable and (continuing is None or continuing.portable)
        self.decisions_before_storage: dict[str, Any] | None = None
        self._members = {id(message) for message in self.view}
        # A pass puts a shallow copy in a message's place and never rebinds a contents list, so
        # the copy is found by the list it shares.
        by_contents = {id(message.contents): message for message in self.view}
        self.contents: list[tuple[Content, ...]] = []
        self.digests: list[str | None] = []
        self.states: list[tuple[Any, ...]] = []
        self.links: list[Any] = []
        self.standing: list[Message | None] = []
        # A handed message the pass replaced is not in the view, so nothing shows what a host
        # does to it afterwards. The state the pass left it in is kept to tell the two apart.
        self.replaced: dict[int, tuple[Any, ...]] = {}
        start = 0
        if continuing is not None:
            self.contents.extend(continuing.contents)
            self.digests.extend(continuing.digests)
            self.states.extend(continuing.states)
            self.links.extend(continuing.links)
            self.replaced.update(continuing.replaced)
            for standing in continuing.standing:
                self.standing.append(self._member(standing, by_contents))
            start = len(continuing.view)
        for index in range(start, len(handed.messages)):
            message = handed.messages[index]
            if _is_transient(message):
                continue
            if id(message) not in self._members:
                self.replaced[len(self.contents)] = _host_state(message)
            self.contents.append(handed.contents[index])
            self.digests.append(handed.digests[index])
            self.states.append(handed.states[index])
            self.links.append(handed.links[index])
            self.standing.append(self._member(handed.stood[index], by_contents))
        self._inserted = self._members.difference(id(m) for m in self.standing if m is not None)
        self._completed = [self._member_state(message) for message in self.view]

    def _member_state(self, message: Message) -> tuple[Any, ...]:
        inserted = content_digest(message.contents) if id(message) in self._inserted else None
        return message.message_id, _host_state(message), _links(message), inserted

    def matches_completed_view(self) -> bool:
        """Return whether the kept messages still carry the layout the pass produced."""
        return len(self.view) == len(self._completed) and all(
            self._member_state(message) == state
            for message, state in zip(self.view, self._completed, strict=True)
        )

    def _member(self, standing: Message | None, by_contents: dict[int, Message]) -> Message | None:
        if standing is None or id(standing) in self._members:
            return standing
        return by_contents.get(id(standing.contents))

    def _unchanged(self, position: int, state: tuple[Any, ...]) -> bool:
        """Return whether a stored message is in a state this view accounts for."""
        if state == self.states[position]:
            return True
        standing = self.standing[position]
        # The host stores the very message the view holds, changed by a pass or by the host.
        if standing is not None and state == _host_state(standing):
            return True
        return state == self.replaced.get(position)

    def begins(self, messages: list[Message]) -> bool:
        """Return whether a list is handed back as the pass left it, with or without more."""
        view = self.view
        return len(messages) >= len(view) and all(
            message is kept for message, kept in zip(messages, view, strict=False)
        )

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
        if not self.matches_completed_view():
            return self._departed("the kept view changed after compaction")
        if self.begins(messages):
            return Resumed(restored=False, changed=False)
        members = self._members
        stored = len(self.contents)
        position = 0
        tail_start = len(messages)
        paired: list[tuple[Message, Message, int]] = []
        portable = self.portable
        for index, message in enumerate(messages):
            if position < stored and _same_contents(message.contents, self.contents[position]):
                if not self._unchanged(position, current.states[index]):
                    return self._departed(
                        f"stored message {position} of {stored} changed between calls"
                    )
                standing = self.standing[position]
                if self.digests[position] != current.digests[index]:
                    if standing is None or (_annotation(standing) or {}).get(
                        SUMMARIZED_BY_SUMMARY_ID_KEY
                    ):
                        return self._departed(f"summary source {position} changed between calls")
                    # A shared edit can still be visible in this live view. A later turn
                    # must rebuild decisions whose sources no longer match the original pass.
                    portable = False
                if standing is not None:
                    # A copy kept under an earlier list shows nothing a host wrote into this one.
                    if not _same_contents(message.contents, standing.contents):
                        return self._departed(
                            f"stored message {position} of {stored} was rewritten after it was kept"
                        )
                    paired.append((standing, message, index))
                position += 1
            elif position == stored:
                tail_start = index
                break
            elif id(message) in members:
                # The host kept something the pass inserted, and the view already holds it.
                continue
            else:
                return self._departed(f"the list departs at stored message {position} of {stored}")
        if position < stored:
            return self._departed(f"{position} of the last call's {stored} messages were handed")
        resumed = apply_view(self.view, messages, current, paired, tail_start)
        if resumed is not None:
            current.portable = portable
        return resumed

    @staticmethod
    def _departed(reason: str) -> None:
        logger.debug("Compaction put its decisions back in full: %s.", reason)


def apply_view(
    view: list[Message],
    messages: list[Message],
    current: Handed,
    paired: list[tuple[Message, Message, int]],
    tail_start: int,
    *,
    omitted: Iterable[int] = (),
) -> Resumed | None:
    """Commit a matched prefix, respecting this call's attribution, token counts and tail IDs."""
    excluded = {index for kept, _, index in paired if kept.additional_properties.get(EXCLUDED_KEY)}
    excluded.update(omitted)
    if not _removals_fit(view, current.messages, excluded):
        return None
    tail = messages[tail_start:]
    handed_now = {
        id(standing): message
        for standing, message, index in paired
        if standing is not message
        and current.states[index] == _host_state(standing)
        and current.links[index] == _links(standing)
    }
    resumed = [handed_now.get(id(message), message) for message in view]
    before = _sent(messages)
    for standing, message, index in paired:
        if standing is message:
            continue
        if id(standing) in handed_now:
            # A positional id is this list's own; the view's is the one its notes refer to.
            message.message_id = standing.message_id
            continue
        current.stood[index] = standing
        if _ATTRIBUTION_KEY in message.additional_properties:
            standing.additional_properties[_ATTRIBUTION_KEY] = message.additional_properties[
                _ATTRIBUTION_KEY
            ]
        else:
            standing.additional_properties.pop(_ATTRIBUTION_KEY, None)
        # A count the host dropped is one it wants taken again.
        if _token_count(message) is None:
            _drop_token_count(standing)
    taken = {message.message_id for message in resumed if message.message_id}
    for message in tail:
        # This list's positional ids were assigned without the view's, so they can collide.
        if message.message_id in taken and not durable_message_id(message.message_id):
            message.message_id = None
    messages[:] = [*resumed, *tail]
    stands_for = {id(standing): message for standing, message, _ in paired}
    return Resumed(restored=True, changed=_sent(messages, stands_for) != before)


def _removals_fit(view: list[Message], messages: list[Message], excluded: set[int]) -> bool:
    """Check whole-group exclusions and their notes against the history this call received."""
    notes = [message for message in view if _is_marker(message)]
    if not excluded and not notes:
        return True
    # Grouping assigns missing positional IDs. A rejected layout must leave the input untouched.
    history = [message if message.message_id else copy(message) for message in messages]
    groups = group_messages(history)
    removable = removable_spans(history, groups)
    by_id = {group["group_id"]: group for group in groups}
    for group in groups:
        start, end = group["start_index"], group["end_index"]
        members = range(start, end + 1)
        if excluded.intersection(members) and (
            (start, end) not in removable or not excluded.issuperset(members)
        ):
            return False
    for note in notes:
        links = note.additional_properties[GROUP_ANNOTATION_KEY]
        group_ids = links[SUMMARY_OF_GROUP_IDS_KEY]
        if len(group_ids) != 1 or (group := by_id.get(group_ids[0])) is None:
            return False
        sources = history[group["start_index"] : group["end_index"] + 1]
        if (
            group["kind"] not in {"tool_call", "assistant_text"}
            or links[SUMMARY_OF_MESSAGE_IDS_KEY] != [message.message_id for message in sources]
            or note.text != AnchoredCompactionStrategy._note_for(history, group)  # pyright: ignore[reportPrivateUsage]
        ):
            return False
    return True
