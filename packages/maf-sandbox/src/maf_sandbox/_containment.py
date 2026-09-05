"""What a site absorbing somebody else's failure catches, written once.

Several places here run code they did not write — a host's observer, a host's context getter, a
backend's declaration property — while already handling something, and each has promised that
what comes back cannot reach the caller.  The rule they share is small and has been got wrong
four separate times: an ``except Exception`` looks exhaustive and misses a cancel; naming the
leaf types misses a group carrying them; catching ``BaseException`` swallows what the caller
meant to propagate.

So it lives here rather than in each site's own tuple.  ``SystemExit`` and ``KeyboardInterrupt``
are the process's control flow and are never absorbed, including when one arrives as a leaf of
a group — which is why a group is unwrapped rather than trusted for being one.
"""

from __future__ import annotations

import asyncio
from typing import cast

__all__ = ["CONTAINED", "escapes_containment"]

#: The catch tuple every containment site uses.  ``SystemExit`` and ``KeyboardInterrupt`` are
#: absent deliberately, so they are never caught in the first place; ``BaseExceptionGroup`` is
#: present because a group of otherwise-absorbable failures is not itself an ``Exception``, and
#: :func:`escapes_containment` is what re-raises the ones that carry process control.
CONTAINED = (Exception, asyncio.CancelledError, GeneratorExit, BaseExceptionGroup)


def escapes_containment(exc: BaseException) -> bool:
    """Whether ``exc`` must be re-raised rather than absorbed.

    True only for a group carrying ``SystemExit`` or ``KeyboardInterrupt``: a bare one of those
    is not in :data:`CONTAINED` and so never reaches a caller of this.
    """
    if not isinstance(exc, BaseExceptionGroup):
        return False
    # Cast, because `isinstance` narrows to a group of *unknown* leaves and the strict checker
    # will not read `subgroup` off one. The leaves are `BaseException` by construction.
    group = cast("BaseExceptionGroup[BaseException]", exc)
    return group.subgroup((SystemExit, KeyboardInterrupt)) is not None
