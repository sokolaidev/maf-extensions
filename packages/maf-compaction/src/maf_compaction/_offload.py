"""Run a pass's synchronous steps on a worker thread, so the host's event loop keeps running.

A pass over a long conversation spends seconds of CPU between its awaits: grouping, token
annotation, replaying saved record edits, shedding. Run on the event loop, that time freezes
every other task the host serves for as long as it lasts. On a worker thread the interpreter
hands the loop the GIL every switch interval (5 ms by default), so the host keeps answering
while the step runs; the step itself is no faster.

A step moved here must touch only the conversation the pass owns and the strategy's own state:
the awaiting pass touches neither until the step returns, and one conversation never runs two
passes at once. A step reads the caller's context variables (``asyncio.to_thread`` copies the
context), and any it sets do not survive the step.

Background record-merge work breaks that rule: its worker is a shallow copy of the strategy
running as a task on the loop, so it shares state with the foreground pass and can resume
between a pass's steps. While such work runs, and inside the worker, steps run where they
are called, as before (:func:`steps_inline`).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import cast

#: Makes the event a step's thread sets when it is over; replaceable so a test can widen the
#: window between that event and the task's own completion.
_new_event: Callable[[], threading.Event] = threading.Event

#: How often a cancelled caller checks whether its step has finished.
_DRAIN_POLL_SECONDS = 0.005

_INLINE: ContextVar[bool] = ContextVar("maf_compaction_inline_steps", default=False)


@contextmanager
def steps_inline(inline: bool = True) -> Generator[None]:
    """Run :func:`off_loop` steps in the calling thread for the enclosed code, when ``inline``."""
    if not inline:
        yield
        return
    token = _INLINE.set(True)
    try:
        yield
    finally:
        _INLINE.reset(token)


async def off_loop[**P, R](step: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Run ``step`` on a worker thread and return its result.

    Cancelling the caller does not abandon the step: the thread cannot be stopped, and a step
    still editing the conversation while the caller unwinds would race whatever runs next. The
    cancellation is delivered once the step has finished, however many cancellations arrive
    meanwhile (a request's cancel followed by a shutdown's, for example). A step whose job had
    not started when the caller was cancelled is abandoned instead, and never runs.
    """
    if _INLINE.get():
        return step(*args, **kwargs)
    # The thread itself says when the step is over. The task below cannot: at shutdown
    # ``asyncio.run`` cancels it directly, and it then reports done while its thread still runs,
    # or drops a job still queued in the executor, whose step then never runs at all.
    finished = _new_event()
    decided = threading.Lock()
    started = False
    abandoned = False

    def run() -> R | None:
        nonlocal started
        with decided:
            if abandoned:
                return None
            started = True
        try:
            return step(*args, **kwargs)
        finally:
            finished.set()

    running = asyncio.ensure_future(asyncio.to_thread(run))
    try:
        # ``asyncio.wait`` never cancels what it waits for, unlike awaiting the task, and unlike
        # ``asyncio.shield`` it leaves no outer future to report the step's failure on its own
        # once the caller is cancelled.
        await asyncio.wait({running})
        if running.cancelled():
            # Shutdown cancelled the step's task, not this caller: drain it the same way.
            raise asyncio.CancelledError
    except asyncio.CancelledError:
        with decided:
            # A step not yet started never will: nothing is left to wait for.
            abandoned = not started
        while not abandoned and not finished.is_set():
            try:
                await asyncio.sleep(_DRAIN_POLL_SECONDS)
            except asyncio.CancelledError:
                continue
        # The thread is done, but the task can still be one callback behind it. Waited for, so its
        # outcome is retrieved here: a step that failed is not reported later as a never-retrieved
        # exception, and the cancellation is what the caller asked for.
        while not abandoned and not running.done():
            try:
                await asyncio.wait({running})
            except asyncio.CancelledError:
                continue
        if running.done() and not running.cancelled():
            running.exception()
        raise
    return cast("R", running.result())
