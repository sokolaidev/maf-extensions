"""Removing a guest path this stack created, and reporting it when that fails.

Apart from :func:`~maf_sandbox.reclaim_run`, which removes a run it can describe with a
:class:`~maf_sandbox.GuestRunLayout`.  What is removed here has no description but the working
directory it sits under, and no caller who could supply one.  The lifetimes behind that live
in ``docs/sandbox/tool-call.md``.

**Guest, not host**, throughout: every path here is addressed the way the sandbox addresses its
own, which for a store with no filesystem under it is still a path — see :meth:`Sandbox.remove`,
whose contract is likewise ``path`` and everything under it.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from ._error_detail import error_detail
from ._protocol import Sandbox, SandboxKey
from .paths import confine_guest_path

__all__ = ["ReclaimFailure", "reclaim_guest_path"]


@dataclass(frozen=True)
class ReclaimFailure:
    """A tool call's own guest path that is still in the sandbox, and why.

    What ``sandboxed_tool``'s ``on_reclaim_failure`` receives.  A data-retention failure rather
    than a tidiness one: ``acquire`` is get-or-create, so what is left stays readable by every
    later call in that sandbox, and disposal is the only remedy left.
    """

    #: The tool whose call left it, as the model sees the name.
    tool: str
    #: The sandbox it is in. Always set: nothing is reported for a call that acquired none,
    #: because such a call wrote nothing.
    key: SandboxKey
    #: The absolute guest path that is still there, with whatever is under it.
    path: str
    #: Why the removal did not happen, in this stack's own words.
    reason: str


async def reclaim_guest_path(
    sandbox: Sandbox, path: str, *, working_directory: str, timeout: float
) -> str | None:
    """Remove ``path`` and everything under it. ``None``, or why it did not happen.

    **No failure of the removal raises.** It runs in a ``finally``, where an exception would
    replace whatever the call was already reporting with a message about cleanup. Cancellation
    is not such a failure: a :class:`~asyncio.CancelledError` or ``GeneratorExit`` at the
    removal is the caller's deadline arriving, and it propagates — containing it would let the
    call return past a bound the host thought it had.

    :meth:`Sandbox.reclaim` rather than :meth:`Sandbox.remove`: the protocol's delete is gated
    by a capability no shipped spec requires, and every backend serves the reclaim. The guards
    below stay here whichever backend answers — a backend's reclaim is the mechanism, not the
    policy.
    """
    try:
        resolved = confine_guest_path(path, working_directory)
    except ValueError as outside:
        return str(outside)
    # Both guards stand on their own, because a recursive delete is irreversible and neither
    # should depend on the caller having derived `path` the way `guest_call_path` does. Two
    # components at minimum: `/` and `/tmp` are the shapes that turn a cleanup into an outage.
    if resolved == posixpath.normpath(working_directory):
        return f"{resolved!r} is the working directory itself"
    if len([part for part in resolved.split("/") if part]) < 2:
        return f"{resolved!r} is too close to the root to remove recursively"
    try:
        await sandbox.reclaim(resolved, working_directory=working_directory, timeout=timeout)
    except Exception as refused:  # noqa: BLE001 — an unreclaimed path is a leak, not a fault
        # Only `Exception`. A `CancelledError` here is the caller's own deadline arriving at
        # this await, and answering with a reason would let the call return past it; the caller
        # records the loss and lets it through. See `maf._reclaim_the_call`.
        return f"the removal call failed: {error_detail(refused)}"
    return None
