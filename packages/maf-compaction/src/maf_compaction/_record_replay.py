"""Source-bound authority for replaying accepted record replacements."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from copy import copy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, cast

from agent_framework import Content, Message
from agent_framework._compaction import (
    EXCLUDE_REASON_KEY,
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    SUMMARIZED_BY_SUMMARY_ID_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
    annotate_message_groups,
)

from ._anchored import EXCLUDE_REASON as SHED_EXCLUDE_REASON
from ._anchored import split_combined_notes
from ._artifacts import USER_FOLD_EXCLUDE_REASON, USER_SUMMARY_EXCLUDE_REASON, is_marker
from ._media import TokenCounts, media_identity
from ._preserve import STANDS_IN_KEY
from ._shed import ShedGroupKeys, durable_message_id, unique_shed_groups

if TYPE_CHECKING:
    from ._usersummary import UserTurnAnchoredSummarizationCompactionStrategy

LINEAGE_KEY: Final[str] = "maf_compaction_record_lineage"


@dataclass
class RecordEdit:
    """An accepted record replacement and the edits its source window already contained."""

    sources: str
    count: int
    text: str | None
    summaries: list[str]
    shed_ids: list[str]
    shed_groups: list[str]
    members: list[str] = field(default_factory=list[str])


@dataclass
class SummaryReplayCache:
    """Share content fingerprints across annotation-only candidate copies."""

    invalid: dict[bool, set[str]] = field(default_factory=dict[bool, set[str]])
    outputs: dict[tuple[str, str], str] = field(default_factory=dict[tuple[str, str], str])
    messages: dict[tuple[str | None, int], tuple[Message, str]] = field(
        default_factory=dict[tuple[str | None, int], tuple[Message, str]]
    )

    def message_key(self, message: Message) -> str:
        key = (message.message_id, id(message.contents))
        if key not in self.messages:
            self.messages[key] = (message, media_identity(message))
        return self.messages[key][1]

    def fork(self) -> SummaryReplayCache:
        return SummaryReplayCache(outputs=self.outputs, messages=self.messages)


#: Why the record phase excludes a tool result a record covers.
RECORD_ANCHOR_EXCLUDE_REASON: Final[str] = "tool_summary_anchored"
#: Exclusions a pass makes of its own and redoes on every pass: a covered tool result and the
#: over-budget shed. Between a batch's records they are fingerprinted as if included, since a
#: record's answer does not depend on them and the next pass may mark them differently.
PASS_EXCLUDE_REASONS: Final = frozenset({RECORD_ANCHOR_EXCLUDE_REASON, SHED_EXCLUDE_REASON})

#: The user phase's own exclusions: a turn it summarised and a summary a fold superseded. Read
#: as included only in a background snapshot's gaps (:func:`window_message_key` with ``users``).
USER_PASS_EXCLUDE_REASONS: Final = frozenset(
    {USER_SUMMARY_EXCLUDE_REASON, USER_FOLD_EXCLUDE_REASON}
)


@dataclass
class MessageKeyCache:
    """Share message fingerprints across the candidate views of one replay call.

    A view's own cache is keyed by position, so a fork, a staged copy and the history they came
    from each fingerprint the same message again. This one is keyed by everything
    :func:`_message_key` reads besides the contents, and by the contents' identity, which a
    copied message shares with its original. Each entry holds the contents it was taken from,
    so that identity cannot pass to another list while the entry lives.

    It must not outlive one replay call. The compaction around a replay rewrites a result or a
    call's arguments in place, which no identity shows; nothing a replay itself calls does.
    The group fingerprints shedding is replayed by, and the token counts taken behind each
    record put back, are shared the same way and for as long.
    """

    entries: dict[tuple[Any, ...], tuple[list[Content], tuple[Content, ...], str]] = field(
        default_factory=dict[tuple[Any, ...], tuple[list[Content], tuple[Content, ...], str]]
    )
    #: Serialized contents by contents identity, so the as-included fingerprint of a message
    #: does not serialize contents its full fingerprint already did.
    serialized: dict[int, tuple[list[Content], tuple[Content, ...], str]] = field(
        default_factory=dict[int, tuple[list[Content], tuple[Content, ...], str]]
    )
    shed_groups: ShedGroupKeys = field(default_factory=ShedGroupKeys)
    counts: TokenCounts = field(default_factory=TokenCounts)

    def key(self, message: Message, *, as_included: bool = False) -> str:
        properties = message.additional_properties
        contents = message.contents
        fields = (
            message.role,
            message.author_name,
            message.message_id,
            properties.get(STANDS_IN_KEY),
            properties.get(LINEAGE_KEY),
        )
        # Equal values of different types serialize differently; those are fingerprinted afresh.
        if not all(value is None or isinstance(value, str) for value in fields):
            return _message_key(message, as_included=as_included)
        excluded = False if as_included else bool(properties.get(EXCLUDED_KEY, False))
        slot = (*fields, excluded, id(contents))
        entry = self.entries.get(slot)
        if (
            entry is not None
            and len(entry[1]) == len(contents)
            and all(held is content for held, content in zip(entry[1], contents, strict=True))
        ):
            return entry[2]
        fingerprint = _message_key(
            message, as_included=as_included, contents_json=self._contents_json(contents)
        )
        self.entries[slot] = (contents, tuple(contents), fingerprint)
        return fingerprint

    def _contents_json(self, contents: list[Content]) -> str:
        held = self.serialized.get(id(contents))
        if (
            held is not None
            and len(held[1]) == len(contents)
            and all(kept is content for kept, content in zip(held[1], contents, strict=True))
        ):
            return held[2]
        serialized = _serialize_contents(contents)
        self.serialized[id(contents)] = (contents, tuple(contents), serialized)
        return serialized


@dataclass
class RecordReplayIndex:
    """Index held outputs and their last accepted edit in one unchanged replay history.

    ``reshapes`` is None until an edit at or before ``last_reshape`` reads it: building it walks
    the whole history, and an edit accepted after the last reshape never does.
    """

    held: list[tuple[int, Message]]
    known: set[tuple[str, str]]
    reshapes: dict[tuple[str, str], Message] | None
    merges: dict[tuple[str, str], tuple[int, Message]]
    last_reshape: int


class _Placed:
    """One message of a history that deferred messages are being put back into."""

    __slots__ = ("home", "message", "original")

    def __init__(self, message: Message, home: int, *, original: bool) -> None:
        self.message = message
        self.home = home
        self.original = original


_Kept = tuple[
    Message,
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any] | None,
    dict[str, Any] | None,
]


@dataclass
class ReplayUndo:
    """A history as a replay was handed it, kept so a refused replay leaves no trace on it.

    A replay over a history that holds package-written records can stop part of the way
    through a chain of staged edits, and must then have changed nothing. It works on the
    history itself and is taken back from this, where it once ran a second time on copies to
    find out. What is kept is what a copied message would have had of its own: its attributes,
    its ``additional_properties`` and its group annotation, each restored in place, since a
    loaded message shares its annotation with the stored one.
    """

    messages: list[Message]
    order: list[Message]
    kept: list[_Kept]

    @classmethod
    def take(cls, messages: list[Message]) -> ReplayUndo | None:
        """Keep ``messages`` as they stand, or None for a history copies would not mirror.

        Copies give every message its own properties and annotation, so two messages sharing
        either would be told apart on the copies and not here; such a history keeps the replay
        on copies. So does a message whose state is not all in its ``__dict__``.
        """
        seen: set[int] = set()
        owned: set[int] = set()
        kept: list[_Kept] = []
        if not all(keeps_state_in_dict(kind) for kind in {type(message) for message in messages}):
            return None
        for message in messages:
            if id(message) in seen:
                continue
            seen.add(id(message))
            properties = _plain_dict(message.additional_properties)
            if properties is None:
                return None
            held: object = properties.get(GROUP_ANNOTATION_KEY)
            annotation = _plain_dict(held)
            if annotation is None and isinstance(held, dict):
                return None
            identities = (
                {id(properties)} if annotation is None else {id(properties), id(annotation)}
            )
            if owned & identities:
                return None
            owned |= identities
            kept.append(
                (
                    message,
                    dict(vars(message)),
                    properties,
                    dict(properties),
                    annotation,
                    None if annotation is None else dict(annotation),
                )
            )
        return cls(messages, list(messages), kept)

    def restore(self) -> None:
        """Put the history back as it was taken."""
        self.messages[:] = self.order
        for message, fields, properties, kept_properties, annotation, kept_annotation in self.kept:
            state = vars(message)
            state.clear()
            state.update(fields)
            properties.clear()
            properties.update(kept_properties)
            if annotation is not None and kept_annotation is not None:
                annotation.clear()
                annotation.update(kept_annotation)


def keeps_state_in_dict(kind: type) -> bool:
    """Return whether an instance of ``kind`` has every attribute in its ``__dict__``."""
    return kind.__dictoffset__ != 0 and not any(
        cls.__dict__.get("__slots__") for cls in kind.__mro__
    )


@dataclass
class ReplayView:
    """Cache source and shed identities until this candidate history changes."""

    messages: list[Message]
    summary_cache: SummaryReplayCache = field(default_factory=SummaryReplayCache)
    message_keys: dict[int, str] = field(default_factory=dict[int, str])
    shed_groups: dict[str, set[str]] | None = None
    exclusions: list[tuple[Message, dict[str, Any]]] = field(
        default_factory=list[tuple[Message, dict[str, Any]]]
    )
    deferred: list[tuple[Message, Message | None]] = field(
        default_factory=list[tuple[Message, Message | None]]
    )
    summary_preview: UserTurnAnchoredSummarizationCompactionStrategy | None = None
    record_replacements: set[int] = field(default_factory=set[int])
    restored_summary_ids: set[str] = field(default_factory=set[str])
    invalid_summary_ids: set[str] = field(default_factory=set[str])
    replacement_keys: dict[tuple[tuple[str, str] | None, str | None, bool], str] = field(
        default_factory=dict[tuple[tuple[str, str] | None, str | None, bool], str]
    )
    key_cache: MessageKeyCache | None = None
    record_index: RecordReplayIndex | None = None
    available_members: set[str] | None = None

    def fork(self) -> ReplayView:
        """Copy mutable history annotations while retaining deferred-message relationships."""
        originals = self.messages + [message for message, _ in self.deferred]
        copies = {id(message): copy(message) for message in originals}
        for message in copies.values():
            message.additional_properties = dict(message.additional_properties)
            annotation = message.additional_properties.get(GROUP_ANNOTATION_KEY)
            if isinstance(annotation, dict):
                message.additional_properties[GROUP_ANNOTATION_KEY] = dict(
                    cast(dict[str, Any], annotation)
                )
        return replace(
            self,
            messages=[copies[id(message)] for message in self.messages],
            message_keys=dict(self.message_keys),
            replacement_keys=dict(self.replacement_keys),
            exclusions=[
                (copies[id(message)], properties) for message, properties in self.exclusions
            ],
            deferred=[
                (copies[id(message)], copies.get(id(after), after))
                for message, after in self.deferred
            ],
            record_replacements={id(copies[identity]) for identity in self.record_replacements},
            summary_preview=copy(self.summary_preview),
            summary_cache=self.summary_cache.fork(),
            record_index=None,
            available_members=None,
        )

    def invalidate(self) -> None:
        self.message_keys.clear()
        self.shed_groups = None
        self.replacement_keys.clear()
        # A fingerprint is of a message, not of the history; only the refusals are of both.
        self.summary_cache = self.summary_cache.fork()
        self.record_index = None
        self.available_members = None

    def message_key(self, index: int) -> str:
        if index not in self.message_keys:
            message = self.messages[index]
            self.message_keys[index] = (
                _message_key(message) if self.key_cache is None else self.key_cache.key(message)
            )
        return self.message_keys[index]

    def matches_replacement(self, index: int, expected: Message) -> bool:
        """Validate a newly built plain record output once per unchanged history."""
        excluded = bool(self.messages[index].additional_properties.get(EXCLUDED_KEY))
        expected.additional_properties[EXCLUDED_KEY] = excluded
        key = (replacement_identity(expected), expected.text, excluded)
        if key not in self.replacement_keys:
            self.replacement_keys[key] = _message_key(expected)
        return self.message_key(index) == self.replacement_keys[key]

    def shed_lookup(self) -> dict[str, set[str]]:
        if self.shed_groups is None:
            keys = None if self.key_cache is None else self.key_cache.shed_groups
            self.shed_groups = {
                key: {message.message_id for message in members if message.message_id}
                for key, members in unique_shed_groups(self.messages, keys=keys)
            }
        return self.shed_groups

    def restore_later(self) -> None:
        """Restore temporarily staged state without undoing the accepted record replacement."""
        for message, properties in self.exclusions:
            for key in (EXCLUDED_KEY, EXCLUDE_REASON_KEY):
                message.additional_properties[key] = properties[key]
            owner = properties.get(GROUP_ANNOTATION_KEY, {}).get(SUMMARIZED_BY_SUMMARY_ID_KEY)
            if owner is not None:
                message.additional_properties[GROUP_ANNOTATION_KEY][
                    SUMMARIZED_BY_SUMMARY_ID_KEY
                ] = owner
        if not self._place_deferred():
            self._scan_for_deferred()
        annotate_message_groups(self.messages)
        self.invalidate()

    def _scan_for_deferred(self) -> None:
        """Put each deferred message back by reading the history for where it goes."""
        for message, following in reversed(self.deferred):
            if id(message) in self.record_replacements:
                regenerated = next(
                    (
                        index
                        for index, item in enumerate(self.messages)
                        if replacement_identity(item) == replacement_identity(message)
                    ),
                    None,
                )
                if regenerated is not None:
                    self.messages[regenerated] = _restored_output(
                        message, self.messages[regenerated]
                    )
                    continue
            sources = message.additional_properties.get(GROUP_ANNOTATION_KEY, {}).get(
                SUMMARY_OF_MESSAGE_IDS_KEY, []
            )
            insertion = next(
                (
                    index
                    for index, item in enumerate(self.messages)
                    if item is following
                    or (
                        following is not None
                        and following.message_id is not None
                        and item.message_id == following.message_id
                    )
                    or item.message_id in sources
                ),
                len(self.messages),
            )
            self.messages.insert(insertion, message)

    def _place_deferred(self) -> bool:
        """Put the deferred messages back where :meth:`_scan_for_deferred` would, in one pass.

        A rebuilt window defers every note and summary accepted after its edit, and reading
        the history once for each is that many walks of it for every edit replayed. The
        history is indexed once instead, by the three things a walk looks for: the following
        message itself, a message id, and a staged output's identity. Each message keeps the
        place it was handed with, and what is put back in front of it queues there, so the
        earliest match is found by place without renumbering anything.

        Returns:
            False, having changed nothing, for ids or source links the index cannot hold.
        """
        staged: list[tuple[Message, Message | None, tuple[str, str] | None, list[Any]]] = []
        for message, following in reversed(self.deferred):
            output = id(message) in self.record_replacements
            identity = replacement_identity(message) if output else None
            annotation = _plain_dict(message.additional_properties.get(GROUP_ANNOTATION_KEY, {}))
            sources = (
                None
                if annotation is None
                else _plain_list(annotation.get(SUMMARY_OF_MESSAGE_IDS_KEY, []))
            )
            if (
                (output and identity is None)
                or sources is None
                or not all(_plain_id(source) for source in sources)
                or not _plain_id(message.message_id)
                or (following is not None and not _plain_id(following.message_id))
            ):
                return False
            staged.append((message, following, identity, sources))
        if not all(_plain_id(item.message_id) for item in self.messages):
            return False
        count = len(self.messages)
        handed = [_Placed(item, index, original=True) for index, item in enumerate(self.messages)]
        queued: list[list[_Placed]] = [[] for _ in range(count + 1)]
        by_object: dict[int, list[_Placed]] = {}
        by_id: dict[str | None, list[_Placed]] = {}
        by_identity: dict[tuple[str, str], list[_Placed]] = {}
        # Only a staged output is looked for by identity, and most windows stage none.
        outputs = any(identity is not None for _, _, identity, _ in staged)

        def lists_of(placed: _Placed) -> list[list[_Placed]]:
            found = [
                by_object.setdefault(id(placed.message), []),
                by_id.setdefault(placed.message.message_id, []),
            ]
            identity = replacement_identity(placed.message) if outputs else None
            if identity is not None:
                found.append(by_identity.setdefault(identity, []))
            return found

        def place(placed: _Placed) -> tuple[int, int, int]:
            if placed.original:
                return placed.home, 1, 0
            return placed.home, 0, _queue_index(queued[placed.home], placed)

        for placed in handed:
            for found in lists_of(placed):
                found.append(placed)
        for message, following, identity, sources in staged:
            if identity is not None:
                regenerated = min(by_identity.get(identity, ()), key=place, default=None)
                if regenerated is not None:
                    for found in lists_of(regenerated):
                        found.remove(regenerated)
                    regenerated.message = _restored_output(message, regenerated.message)
                    for found in lists_of(regenerated):
                        found.append(regenerated)
                    continue
            matches: list[_Placed] = []
            if following is not None:
                matches.extend(by_object.get(id(following), ()))
                if following.message_id is not None:
                    matches.extend(by_id.get(following.message_id, ()))
            for source in sources:
                matches.extend(by_id.get(source, ()))
            anchor = min(matches, key=place, default=None)
            placed = _Placed(message, count if anchor is None else anchor.home, original=False)
            queue = queued[placed.home]
            if anchor is None or anchor.original:
                queue.append(placed)
            else:
                queue.insert(_queue_index(queue, anchor), placed)
            for found in lists_of(placed):
                found.append(placed)
        self.messages[:] = [
            item.message for index, placed in enumerate(handed) for item in (*queued[index], placed)
        ] + [item.message for item in queued[count]]
        return True


def replacement_identity(message: Message) -> tuple[str, str] | None:
    """Identify package-written record outputs without treating their bodies as authority."""
    for key in (STANDS_IN_KEY, LINEAGE_KEY):
        identity = message.additional_properties.get(key)
        if isinstance(identity, str) and identity:
            return key, identity
    return None


def _restored_output(message: Message, current: Message) -> Message:
    """Return a staged output as it was held, under the state its regenerated form now has."""
    restored = copy(message)
    restored.additional_properties = dict(message.additional_properties)
    for key in (EXCLUDED_KEY, EXCLUDE_REASON_KEY):
        restored.additional_properties.pop(key, None)
    restored.additional_properties.update(current.additional_properties)
    return restored


def _plain_id(value: object) -> bool:
    return value is None or type(value) is str


def _plain_dict(value: object) -> dict[str, Any] | None:
    return cast(dict[str, Any], value) if type(value) is dict else None


def _plain_list(value: object) -> list[Any] | None:
    return cast(list[Any], value) if type(value) is list else None


def _queue_index(queue: list[_Placed], placed: _Placed) -> int:
    return next(index for index, other in enumerate(queue) if other is placed)


def source_key(
    messages: list[Message],
    groups: list[dict[str, Any]],
    *,
    keys: MessageKeyCache | None = None,
    wanted_lineages: set[str] | None = None,
) -> str | None:
    """Identify a replay's source window and originally excluded later reasoning."""
    key = source_lineage(messages, groups, keys=keys)
    if key is None or (wanted_lineages is not None and key not in wanted_lineages):
        return None
    excluded_reasoning = {
        _reasoning_key(message)
        for message in messages[groups[-1]["end_index"] + 1 :]
        if message.additional_properties.get(EXCLUDED_KEY, False)
        and any(content.type == "text_reasoning" for content in message.contents)
    }
    if None in excluded_reasoning:
        # Content cannot identify old reasoning once unrelated metadata changes.
        return None
    return "v2:" + key + ":" + ",".join(sorted(value for value in excluded_reasoning if value))


def source_members(
    messages: list[Message],
    groups: list[dict[str, Any]],
    *,
    keys: MessageKeyCache | None = None,
) -> list[str]:
    """Fingerprint included batch members whose owned exclusions can be reconstructed."""
    return [
        _message_key(message) if keys is None else keys.key(message)
        for group in groups
        for message in messages[group["start_index"] : group["end_index"] + 1]
        if not message.additional_properties.get(EXCLUDED_KEY)
    ]


def source_lineage(
    messages: list[Message],
    groups: list[dict[str, Any]],
    *,
    keys: MessageKeyCache | None = None,
) -> str | None:
    """Fingerprint identified sources independently of later history.

    ``keys`` shares the message fingerprints of the replay call that is putting the edit back.
    """
    return _source_key(messages, groups, {}, keys)


def _serialize_contents(contents: list[Content]) -> str:
    return json.dumps(
        [content.to_dict(exclude={"raw_representation"}) for content in contents],
        sort_keys=True,
        default=str,
    )


def _message_key(
    message: Message, *, as_included: bool = False, contents_json: str | None = None
) -> str:
    """Fingerprint a message; ``as_included`` reads its excluded flag as False.

    The contents, the costly part, are serialized apart and spliced in, which gives the
    bytes ``json.dumps`` gives the whole list, so a fingerprint is what it always was and
    ``contents_json`` can be shared between a message's two fingerprints.
    """
    fields = [
        message.role,
        message.author_name,
        message.message_id
        if durable_message_id(message.message_id)
        and not message.additional_properties.get(STANDS_IN_KEY)
        and not message.additional_properties.get(LINEAGE_KEY)
        else None,
        message.additional_properties.get(STANDS_IN_KEY),
        message.additional_properties.get(LINEAGE_KEY),
        False if as_included else bool(message.additional_properties.get(EXCLUDED_KEY, False)),
    ]
    if contents_json is None:
        contents_json = _serialize_contents(message.contents)
    payload = json.dumps(fields, sort_keys=True, default=str)[:-1] + ", " + contents_json + "]"
    return hashlib.sha256(payload.encode()).hexdigest()


def _is_pass_exclusion(message: Message, *, users: bool = False) -> bool:
    """Whether ``message`` is excluded for a pass's own reason (:data:`PASS_EXCLUDE_REASONS`).

    With ``users``, the user phase's reasons (:data:`USER_PASS_EXCLUDE_REASONS`) count too.
    Never reasoning: replay authority over reasoning reads its inclusion, whoever excluded it.
    """
    properties = message.additional_properties
    reason = properties.get(EXCLUDE_REASON_KEY)
    return (
        bool(properties.get(EXCLUDED_KEY, False))
        and (reason in PASS_EXCLUDE_REASONS or (users and reason in USER_PASS_EXCLUDE_REASONS))
        and not any(content.type == "text_reasoning" for content in message.contents)
    )


def window_message_key(message: Message, *, users: bool = False) -> str:
    """Fingerprint a message between a batch's records, a pass's own exclusion read as included.

    The one rule for both places that fingerprint those messages: a batch's source key and the
    gap a background snapshot collapses them into, so neither changes when a later pass covers or
    sheds a tool round differently. ``users`` also reads the user phase's exclusions as
    included, for the gaps alone: the phase runs before a pass applies ready background answers,
    after the worker snapshotted the list. A kept edit's key keeps reading them, so an edit is
    not put back across a summary that has changed since.
    """
    return _message_key(message, as_included=_is_pass_exclusion(message, users=users))


def _reasoning_key(message: Message) -> str | None:
    if durable_message_id(message.message_id):
        return hashlib.sha256(str(message.message_id).encode()).hexdigest()
    return None


def _source_key(
    messages: list[Message],
    groups: list[dict[str, Any]],
    message_keys: dict[int, str],
    key_cache: MessageKeyCache | None = None,
    window_keys: dict[int, str] | None = None,
    *,
    skip_notes: bool = True,
) -> str | None:
    """Fingerprint a batch: its records, the messages between them, and the layout.

    A message between the records that a pass excluded of its own accord
    (:func:`_is_pass_exclusion`) is fingerprinted as if included, so a later pass that covers or
    sheds it differently still finds the batch, and a batch whose in-between messages are all
    included keeps the key it always had. A shed's note between them (:func:`is_marker`) is left
    out, and so is its place in the layout: a pass puts the notes back only when it sheds, so the
    list a background worker snapshots carries them and the next pass's copy does not yet. With
    no note there the key is what it always was; ``skip_notes=False`` gives a note's window its
    key from before, for an edit accepted then. Every other exclusion there, and the records'
    own, stays in the key. ``window_keys`` caches, by index for one unchanging history, the
    as-if-included fingerprint of a pass exclusion and an empty string for any other message, so
    the many windows of one replay decide each message once.
    """
    for group in groups:
        members = messages[group["start_index"] : group["end_index"] + 1]
        if not any(
            durable_message_id(message.message_id)
            or message.additional_properties.get(STANDS_IN_KEY)
            or message.additional_properties.get(LINEAGE_KEY)
            or any(
                content.type == "function_call" and content.call_id for content in message.contents
            )
            for message in members
        ):
            return None
    start, end = groups[0]["start_index"], groups[-1]["end_index"]
    members = {
        index for group in groups for index in range(group["start_index"], group["end_index"] + 1)
    }
    if window_keys is None:
        window_keys = {}
    keys: list[str] = []
    positions: dict[int, int] = {}
    for index in range(start, end + 1):
        message = messages[index]
        if index not in members:
            if skip_notes and is_marker(message):
                continue
            positions[index] = len(keys)
            window_key = window_keys.get(index)
            if window_key is None:
                window_key = window_keys[index] = (
                    (
                        _message_key(message, as_included=True)
                        if key_cache is None
                        else key_cache.key(message, as_included=True)
                    )
                    if _is_pass_exclusion(message)
                    else ""
                )
            if window_key:
                keys.append(window_key)
                continue
        positions[index] = len(keys)
        if index not in message_keys:
            message_keys[index] = (
                _message_key(message) if key_cache is None else key_cache.key(message)
            )
        keys.append(message_keys[index])
    payload = [
        [[positions[group["start_index"]], positions[group["end_index"]]] for group in groups],
        keys,
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _notes_before(messages: list[Message]) -> list[int]:
    """How many shed notes precede each index, so a window's are counted in constant time."""
    counts = [0]
    for message in messages:
        counts.append(counts[-1] + is_marker(message))
    return counts


def replay_match(
    messages: list[Message],
    groups: list[dict[str, Any]],
    accepted: Collection[tuple[str, int]],
    *,
    message_keys: dict[int, str] | None = None,
    key_cache: MessageKeyCache | None = None,
) -> tuple[list[dict[str, Any]], tuple[str, int]] | None:
    """Find an unambiguous accepted batch; shared fingerprints require an unchanged history."""
    matches: dict[tuple[str, int], list[list[dict[str, Any]]]] = {}
    if message_keys is None:
        message_keys = {}
    certificates: dict[tuple[str, int], list[tuple[str, set[str]]]] = {}
    for certificate, count in accepted:
        version, _, payload = certificate.partition(":")
        key, separator, excluded = payload.partition(":")
        if version == "v2" and separator and count > 0:
            certificates.setdefault((key, count), []).append(
                (certificate, set(excluded.split(",")) if excluded else set())
            )
    included_reasoning = {
        index: _reasoning_key(message)
        for index, message in enumerate(messages)
        if not message.additional_properties.get(EXCLUDED_KEY, False)
        and any(content.type == "text_reasoning" for content in message.contents)
    }

    def consider(found: list[tuple[str, set[str]]], run: list[dict[str, Any]], count: int) -> None:
        for certificate, excluded in found:
            if not any(
                identity in excluded
                for index, identity in included_reasoning.items()
                if index > run[-1]["end_index"]
            ):
                matches.setdefault((certificate, count), []).append(run)

    window_keys: dict[int, str] = {}
    for count in sorted({count for _, count in certificates}, reverse=True):
        for start in range(len(groups) - count + 1):
            run = groups[start : start + count]
            key = _source_key(messages, run, message_keys, key_cache, window_keys)
            if key is not None:
                consider(certificates.get((key, count), []), run, count)
    # Only an edit no window matched is looked for again, by the older key a window holding a
    # note had before notes were skipped: a session whose edits all match pays nothing more.
    unmatched = {
        (key, count): found
        for (key, count), found in certificates.items()
        if not any((certificate, count) in matches for certificate, _ in found)
    }
    if not unmatched:
        return _unambiguous(matches)
    notes = _notes_before(messages)
    split: tuple[list[Message], dict[int, int]] | None | bool = False
    split_keys: dict[int, str] = {}
    split_windows: dict[int, str] = {}
    for count in sorted({count for _, count in unmatched}, reverse=True):
        for start in range(len(groups) - count + 1):
            run = groups[start : start + count]
            if notes[run[-1]["end_index"] + 1] == notes[run[0]["start_index"]]:
                continue
            if split is False:
                # Built once, for the first window holding a note.
                split = split_combined_notes(messages)
            window = range(run[0]["start_index"], run[-1]["end_index"] + 1)
            if split and any(index not in split[1] for index in window):
                view, at = split
                legacy = _source_key(
                    view,
                    [
                        {**g, "start_index": at[g["start_index"]], "end_index": at[g["end_index"]]}
                        for g in run
                    ],
                    split_keys,
                    None,
                    split_windows,
                    skip_notes=False,
                )
            else:
                legacy = _source_key(
                    messages, run, message_keys, key_cache, window_keys, skip_notes=False
                )
            if legacy is not None:
                consider(unmatched.get((legacy, count), []), run, count)
    return _unambiguous(matches)


def _unambiguous(
    matches: dict[tuple[str, int], list[list[dict[str, Any]]]],
) -> tuple[list[dict[str, Any]], tuple[str, int]] | None:
    """The earliest, then widest, edit that exactly one window matched."""
    candidates = [(runs[0], key) for key, runs in matches.items() if len(runs) == 1]
    return min(
        candidates,
        key=lambda match: (match[0][0]["start_index"], -len(match[0])),
        default=None,
    )
