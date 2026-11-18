"""Optional observation of record merge admission and application."""

from __future__ import annotations

from collections.abc import Callable, Collection, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
from typing import TYPE_CHECKING, Literal

from ._record_replay import LINEAGE_KEY

if TYPE_CHECKING:
    from agent_framework import Message

type RecordMergePhase = Literal["foreground", "background", "apply"]
type RecordMergeObserver = Callable[[RecordMergePhase, str, Mapping[str, int | str]], None]
type RecordReduction = Literal["merge", "rewrite"]

record_merge_observer: ContextVar[RecordMergeObserver | None] = ContextVar(
    "record_merge_observer", default=None
)
tracked_merge_values: ContextVar[Collection[str]] = ContextVar("tracked_merge_values", default=())


def record_merge_identity(message: Message) -> str | None:
    """Correlate the content and source lineage of a merged assistant record."""
    lineage = message.additional_properties.get(LINEAGE_KEY)
    if (
        message.role != "assistant"
        or not isinstance(lineage, str)
        or not lineage
        or not message.text
    ):
        return None
    return f"{lineage}:{sha256(message.text.encode()).hexdigest()}"


@contextmanager
def observe_record_merges(
    observer: RecordMergeObserver | None, *, tracked_values: Collection[str] = ()
) -> Generator[None]:
    """Observe merges made in this context, including tasks it starts.

    Callbacks receive a phase, an event name and counts or reason codes. The
    accepted and prepared events can include transient identifiers for matching
    outgoing history and worker results. Treat them as in-process data. Optional
    tracked identifiers are counted as whole tokens and never emitted.

    A callback can run on a worker thread, because a pass runs its synchronous steps there
    (``maf_compaction._offload``). One session's callbacks never overlap; an observer shared
    by sessions that compact at the same time must be thread-safe.
    """
    token = record_merge_observer.set(observer)
    values_token = tracked_merge_values.set(tracked_values)
    try:
        yield
    finally:
        tracked_merge_values.reset(values_token)
        record_merge_observer.reset(token)
