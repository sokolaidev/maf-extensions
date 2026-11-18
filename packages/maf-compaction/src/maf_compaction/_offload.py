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
import functools
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar

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
    meanwhile (a request's cancel followed by a shutdown's, for example).
    """
    if _INLINE.get():
        return step(*args, **kwargs)
    running = asyncio.ensure_future(asyncio.to_thread(functools.partial(step, *args, **kwargs)))
    try:
        return await asyncio.shield(running)
    except asyncio.CancelledError:
        while not running.done():
            try:
                await asyncio.wait({running})
            except asyncio.CancelledError:
                continue
        if not running.cancelled():
            # Retrieved so a step that failed is not reported as a never-retrieved exception;
            # the cancellation is what the caller asked for.
            running.exception()
        raise
