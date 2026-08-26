"""Thread-delete participant: reclaim a conversation's sandboxes when it is deleted.

Duck-typed on purpose — it exposes ``async purge_scoped_thread(scope, thread_id)`` and
nothing else, so a host awaits it without importing this module or knowing what it is.

It used to be a backend-specific class.  It is not backend-specific: reclaiming a deleted
conversation's compute is a router concern, and the router already asks every registered
backend.
"""

from __future__ import annotations

import logging

from ._protocol import ScopePurge
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

    async def purge_scoped_thread(self, scope: str, thread_id: str) -> ScopePurge:
        """Delete every sandbox for ``(scope, thread_id)``; returns how many, and what stayed.

        Errors are swallowed by the router: purge must not fail a delete, and the backends'
        auto-delete timers remain as the fallback.  :attr:`~maf_sandbox.ScopePurge.undisposed`
        is how a host hears that the delete it just served did not land: the sandboxes of a
        conversation a user deleted are the ones least acceptable to leave running, and a count
        cannot say it — zero reads the same whether there was nothing to delete or nothing
        worked.
        """
        purge = await self._router.dispose_scope(scope, thread_id)
        if purge.disposed:
            logger.info(
                "sandbox purge: deleted %d sandbox(es) for thread %s", purge.disposed, thread_id
            )
        if purge.undisposed is not None:
            logger.warning(
                "sandbox purge: thread %s is not fully deleted: %s", thread_id, purge.undisposed
            )
        return purge
