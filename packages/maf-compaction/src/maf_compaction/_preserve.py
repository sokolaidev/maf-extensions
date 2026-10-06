"""One annotation that says a message must survive compaction intact.

**A strategy must not destroy its own output.**
:class:`~._toolsummary.ToolResultAnchoredSummarizationCompactionStrategy` asks the model to
write everything that matters from the earlier tool results into one tool result of its own,
deletes the groups that record replaced, and hands what is left to a fallback strategy that
shortens and sheds tool results. The record is a tool result. Every deletion it licences is
safe only because the record is there, so trimming it afterwards discards the sole surviving
copy of everything already deleted, at the moment the conversation is under most pressure.
This flag is what stops the fallback treating the record as ordinary trimmable bulk.

**Preserved is not excluded, and the difference is the whole design.** ``EXCLUDED_KEY`` in
``agent_framework._compaction`` says "this message is not being sent", so an excluded message
costs nothing and every token count skips it. A preserved message *is* being sent, is counted
in full, and pays for itself like anything else; the flag says only that no strategy may buy
budget by making it smaller. The two are orthogonal, and a strategy consulting the wrong one
would either send a message it meant to drop or price the prompt as though the record were
free.

**A strategy that cannot reach its ceiling must stop, not spin.** Because a preserved message
still counts, protecting it can leave a conversation over budget with nothing left that may be
removed. The contract in that case is to stop and let the caller see the prompt is over the
ceiling -- the same thing the anchored strategy already does when the anchors alone exceed it.
A shed step that kept re-examining a band it is no longer allowed to touch would loop, and a
step that silently gave up while reporting success would hand the provider a prompt it will
reject. Both are worse than an honest overflow.

**Why a module of its own.** ``_toolsummary`` marks the record and ``_anchored`` honours the
mark, and ``_toolsummary`` already imports ``_anchored`` for its default fallback. Putting the
key in either of them would either invert that dependency or create a cycle. The naming and
the annotate/read pattern follow ``EXCLUDED_KEY``, ``EXCLUDE_REASON_KEY`` and ``set_excluded``
deliberately: a reader who knows the framework's convention should not have to learn a second
one to read this.

**The mark does not survive storage, and must be re-applied.** Compaction runs against a
freshly loaded conversation on every turn and ``additional_properties`` set by a previous pass
is not there when the next one starts -- which is why the framework's own exclusion flags are
re-derived each time too. Whoever owns a message that must be protected therefore re-marks it
on every pass, as ``_toolsummary`` does when it observes a record, rather than marking it once
and trusting it to persist. The one exception is a message a model call carries in rather than
loads, whose flags are stored with it; :func:`removable_whole` is what that costs an exclusion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from agent_framework import Message

__all__ = [
    "PRESERVED_KEY",
    "PRESERVE_REASON_KEY",
    "any_preserved",
    "is_preserved",
    "removable_whole",
    "set_preserved",
]

#: The key ``SessionContext.extend_messages`` stamps on the copy of every message a history
#: provider loads. Read here, and only here, to tell a message loaded for this call from one the
#: call itself carries; see :func:`removable_whole`.
_ATTRIBUTION_KEY: Final[str] = "_attribution"

#: Marks a message no strategy may shorten, drop or shed. Named after ``EXCLUDED_KEY``, and
#: read the same way: absent means false, so an unannotated conversation behaves exactly as it
#: did before this existed.
PRESERVED_KEY: Final[str] = "_preserved"

#: Why a message was preserved, so a caller inspecting a conversation can tell which strategy
#: claimed it. Mirrors ``EXCLUDE_REASON_KEY``.
PRESERVE_REASON_KEY: Final[str] = "_preserve_reason"


def set_preserved(message: Message, *, preserved: bool, reason: str | None = None) -> bool:
    """Mark ``message`` as protected from removal, or release it.

    Args:
        message: The message to annotate, mutated in place.

    Keyword Args:
        preserved: True to protect it, False to release it.
        reason: Recorded alongside the flag when given, so a conversation can be read back and
            the protecting strategy named. Left untouched when None, which keeps an earlier
            reason rather than blanking it.

    Returns:
        True if the flag's value changed, matching ``set_excluded``'s contract so a caller can
        fold it into a "did anything change" tally without a special case.
    """
    changed = bool(message.additional_properties.get(PRESERVED_KEY, False)) != preserved
    if changed:
        message.additional_properties[PRESERVED_KEY] = preserved
    if reason is not None:
        message.additional_properties[PRESERVE_REASON_KEY] = reason
    return changed


def is_preserved(message: Message) -> bool:
    """Return whether ``message`` may not be shortened, dropped or shed.

    Args:
        message: The message to inspect.

    Returns:
        True when the message carries the protection.
    """
    return bool(message.additional_properties.get(PRESERVED_KEY, False))


def any_preserved(messages: Iterable[Message]) -> bool:
    """Return whether any of ``messages`` is protected.

    Group-level removal is all-or-nothing -- a tool call without its result is a malformed
    conversation on most providers -- so a group is protected as soon as one of its members is,
    rather than only when all of them are. That is also the direction this package errs in
    everywhere else: keeping too much costs tokens a reader can see on the bill, and keeping
    too little costs a fact with no trace of where it went.

    Args:
        messages: The messages to inspect, typically one group's span.

    Returns:
        True when at least one is protected.
    """
    return any(is_preserved(message) for message in messages)


def removable_whole(
    messages: Sequence[Message], spans: Sequence[Mapping[str, Any]], span: Mapping[str, Any]
) -> bool:
    """Return whether excluding ``span`` removes its calls and their outputs together, everywhere.

    The rule every exclusion site in this package answers to: a function call and its output are
    removed together or not at all. Excluding a group's whole span keeps them together in the
    list the strategy is given, and that is not enough, for two reasons -- in each the removal
    would reach one half and not the other, and the provider refuses the call left without its
    output (``No tool output found for function call``) or the output left without its call.

    **A span whose members will not all keep the flag.** On the harness's live path each model
    call is compacted over a list built from two sources: the history provider's messages, which
    ``SessionContext.extend_messages`` loads as copies carrying ``_attribution``, and the messages
    the call itself carries in -- after a tool call, its result -- which are the very objects the
    history stores once the call returns. An exclusion flag set on a copy lasts for that call; one
    set on a carried-in message is stored with it. A group whose call was loaded and whose result
    was carried in therefore loses only its result for good, and the next call sends the call
    alone, which the provider refuses; merging a record on the call that carried the record's
    result in produces exactly that. Such a group is not removable on that call;
    on the next one its result has been stored and is loaded like the rest, and the group is
    removable whole again. The mark in :mod:`agent_framework._sessions` is read rather than any
    position, because it is what decides whether a flag lasts.

    **A span linked to another.** ``group_messages`` gives a call and an output that are not
    adjacent one ``group_id`` over two spans; excluding one span leaves the other. Nothing in this
    package separates a call from its output -- every insertion lands on a group boundary -- so
    this is refused rather than handled, and a caller that meets it keeps the group.

    Args:
        messages: The conversation ``spans`` index into.
        spans: Every span of the conversation, from ``group_messages``.
        span: The span a caller means to exclude.

    Returns:
        True when excluding the span's messages removes each call with its output, in the list
        the call is sent and in the stored history alike.
    """
    if any(
        other["group_id"] == span["group_id"] and other["start_index"] != span["start_index"]
        for other in spans
    ):
        return False
    members = messages[span["start_index"] : span["end_index"] + 1]
    loaded = [_ATTRIBUTION_KEY in message.additional_properties for message in members]
    return all(loaded) or not any(loaded)
