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

    Cleanup after host death depends on a configured platform lifecycle policy or an independent
    operator sweep; this participant runs only when the host calls it.
    """

    def __init__(self, router: SandboxRouter) -> None:
        self._router = router

    async def purge_scoped_thread(self, scope: str, thread_id: str) -> ScopePurge:
        """Delete every sandbox for ``(scope, thread_id)``; returns how many, and what stayed.

        The router reports backend failures in :attr:`~maf_sandbox.ScopePurge.undisposed` so the
        host can arrange retries; a zero count alone does not establish complete cleanup.
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
