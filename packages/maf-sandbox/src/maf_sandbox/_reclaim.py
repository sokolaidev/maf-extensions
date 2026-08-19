"""Removing a guest path this stack created, and reporting it when that fails.

Apart from :func:`~maf_sandbox.reclaim_run`, which removes a run it can describe with a
:class:`~maf_sandbox.GuestRunLayout`.  What is removed here has no description but the working
directory it sits under, and no caller who could supply one.  The lifetimes behind that live
in ``docs/design/call-lifetime.md``.

**Guest, not host**, throughout: every path here is addressed the way the sandbox addresses its
own, which for a store with no filesystem under it is still a path — see :meth:`Sandbox.remove`,
whose contract is likewise ``path`` and everything under it.
"""

from __future__ import annotations

import posixpath
import shlex
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
    #: The sandbox it is in, or ``None`` when the call never acquired one.
    key: SandboxKey | None
    #: The absolute guest path that is still there, with whatever is under it.
    path: str
    #: Why the removal did not happen, in this stack's own words.
    reason: str


async def reclaim_guest_path(
    sandbox: Sandbox, path: str, *, working_directory: str, timeout: float
) -> str | None:
    """Remove ``path`` and everything under it. ``None``, or why it did not happen.

    **Never raises.** It runs in a ``finally``, where an exception would replace whatever the
    call was already reporting with a message about cleanup.

    ``rm -rf`` because that is the one removal every backend serving a kind can do today: the
    protocol's own delete is gated by a capability no shipped spec requires. Which mechanism to
    use is not a question this stack can answer well from here — see #477.
    """
    try:
        resolved = confine_guest_path(path, working_directory)
    except ValueError as outside:
        return str(outside)
    if resolved == posixpath.normpath(working_directory):
        # An irreversible recursive delete, so the guard does not rely on the caller having
        # derived `path` the way `guest_call_path` does.
        return f"{resolved!r} is the working directory itself"
    try:
        removed = await sandbox.exec(
            f"rm -rf {shlex.quote(resolved)}",
            working_directory=working_directory,
            timeout=timeout,
        )
    except Exception as refused:  # noqa: BLE001 — an unreclaimed directory is a leak, not a fault
        return f"the removal call failed: {error_detail(refused)}"
    if removed.exit_code != 0:
        detail = removed.stderr.strip()
        refused = f"the guest refused it: rm exited {removed.exit_code}"
        return f"{refused} — {detail}" if detail else refused
    return None
