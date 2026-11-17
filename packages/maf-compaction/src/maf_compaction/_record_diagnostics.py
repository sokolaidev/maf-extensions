"""Optional observation of record merge admission and application."""

from collections.abc import Callable, Collection, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

type RecordMergePhase = Literal["foreground", "background", "apply"]
type RecordMergeObserver = Callable[[RecordMergePhase, str, Mapping[str, int | str]], None]

record_merge_observer: ContextVar[RecordMergeObserver | None] = ContextVar(
    "record_merge_observer", default=None
)
tracked_merge_values: ContextVar[Collection[str]] = ContextVar("tracked_merge_values", default=())


@contextmanager
def observe_record_merges(
    observer: RecordMergeObserver | None, *, tracked_values: Collection[str] = ()
) -> Generator[None]:
    """Observe merges made in this context, including tasks it starts.

    Callbacks receive a phase, an event name and counts or reason codes. The
    accepted event includes a transient replacement fingerprint for matching
    the outgoing history. Treat it as in-process correlation data. Optional
    tracked values are counted by exact quoted text and never emitted.
    """
    token = record_merge_observer.set(observer)
    values_token = tracked_merge_values.set(tracked_values)
    try:
        yield
    finally:
        tracked_merge_values.reset(values_token)
        record_merge_observer.reset(token)
