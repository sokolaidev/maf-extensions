"""Thread-delete participant: reclaim a conversation's sandboxes when it is deleted.

Duck-typed on purpose — it exposes ``async purge_scoped_thread(scope, thread_id)`` and
nothing else, so a host awaits it without importing this module or knowing what it is.

It used to be a backend-specific class.  It is not backend-specific: reclaiming a deleted
conversation's compute is a router concern, and the router already asks every registered
backend.
"""

from __future__ import annotations

import logging

from ._router import SandboxRouter

logger = logging.getLogger(__name__)

__all__ = ["SandboxPurger"]


class SandboxPurger:
    """Deletes a thread's sandboxes on conversation delete.

    Without it a deleted conversation's sandboxes stay billable until the auto-delete timer
    fires — and that timer is measured in minutes per sandbox, per agent, per conversation.
    """

    def __init__(self, router: SandboxRouter) -> None:
        self._router = router

    async def purge_scoped_thread(self, scope: str, thread_id: str) -> int:
        """Delete every sandbox for ``(scope, thread_id)``; returns how many.

        Errors are swallowed by the router: purge must not fail a delete, and the backends'
        auto-delete timers remain as the fallback.
        """
        count = await self._router.dispose_scope(scope, thread_id)
        if count:
            logger.info("sandbox purge: deleted %d sandbox(es) for thread %s", count, thread_id)
        return count
