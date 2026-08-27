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
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from ._error_detail import error_detail
from ._protocol import Sandbox, SandboxKey
from .paths import confine_guest_path

__all__ = [
    "DEFAULT_RECLAIM_CONFIG",
    "DisposalOutcome",
    "FailedReclaimPolicy",
    "ReclaimConfig",
    "ReclaimFailure",
    "note_unclean",
    "reclaim_guest_path",
]

#: What the framework did about a sandbox it could not clean, as ``ReclaimFailure`` reports
#: it. ``"disposed"``: the sandbox is gone, and the conversation's next call starts cold.
#: ``"failed"``: the disposal did not land, and the router refuses that key until one does.
#: ``"kept"``: the host opted down with ``FailedReclaimPolicy.KEEP``, so the sandbox
#: stays warm with the data in it.
DisposalOutcome = Literal["disposed", "failed", "kept"]


class FailedReclaimPolicy(StrEnum):
    """What the framework does when a tool call cannot leave its sandbox clean."""

    #: Discard the sandbox to prevent data leaks across turns (the default posture).
    DISPOSE = "dispose"
    #: Preserve the sandbox with the data in it for debugging or forensic inspection.
    KEEP = "keep"


@dataclass(frozen=True)
class ReclaimConfig:
    """Host-wide policy and handlers for tool call reclaim."""

    timeout: float = 30.0
    failed_reclaim_policy: FailedReclaimPolicy = FailedReclaimPolicy.DISPOSE
    on_failure: Callable[[ReclaimFailure], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "failed_reclaim_policy",
            FailedReclaimPolicy(str(self.failed_reclaim_policy)),
        )


DEFAULT_RECLAIM_CONFIG = ReclaimConfig()

#: One note about a sandbox left unclean in a way no removal can answer: the sandbox it is
#: about (by identity) and why. Kept per note so a call that acquired more than one disposes
#: only the sandbox a note names, never a sibling that stayed clean.
_Note = tuple[object, str]

#: The running call's notes, set by ``sandboxed_tool`` for the call's duration, ``None`` outside.
_UNCLEAN: ContextVar[list[_Note] | None] = ContextVar("maf_sandbox_unclean", default=None)


def open_unclean_notes() -> tuple[list[_Note], Token[list[_Note] | None]]:
    """Start a tool call's notes. ``sandboxed_tool`` keeps the list and resets with the token."""
    notes: list[_Note] = []
    return notes, _UNCLEAN.set(notes)


def close_unclean_notes(token: Token[list[_Note] | None]) -> None:
    """End the call's notes: whatever is noted from here on belongs to no call."""
    _UNCLEAN.reset(token)


def note_unclean(sandbox: object, reason: str) -> None:
    """Record that the running tool call left ``sandbox`` in a state no removal can clean.

    For a transport that stopped a program and cannot say the whole process tree went with
    it: what survived can write a path back after the call's directory is removed, so the
    removal alone does not make the sandbox clean. ``sandboxed_tool`` reads the notes when the
    call ends and disposes over the sandbox each names — by identity, so a call that acquired a
    second sandbox does not dispose it over the first's overrun. A no-op outside a tool call —
    a transport driven directly has no call to note.
    """
    notes = _UNCLEAN.get()
    if notes is not None:
        notes.append((sandbox, reason))


@dataclass(frozen=True)
class ReclaimFailure:
    """A tool call's own guest path the framework acted on, and why.

    What ``sandboxed_tool``'s ``on_reclaim_failure`` receives, after the framework has acted.
    A data-retention failure rather than a tidiness one: ``acquire`` is get-or-create, so what
    is left stays readable by every later call in that sandbox, and disposal is the only
    remedy. The framework disposes by default and says so in :attr:`disposal`; a host that
    opted down with ``FailedReclaimPolicy.KEEP`` is told the sandbox was kept.
    """

    #: The tool whose call left it, as the model sees the name.
    tool: str
    #: The sandbox it is in. Always set: nothing is reported for a call that acquired none,
    #: because such a call wrote nothing.
    key: SandboxKey
    #: The absolute guest path the call left behind — its *affected* path, not one guaranteed
    #: to still exist: a landed disposal (``disposal == "disposed"``) took the whole sandbox,
    #: and a stop-only note names a path a successful reclaim already removed.
    path: str
    #: Why the sandbox is not clean, in this stack's own words: the removal that did not
    #: happen, or the stop that did not reach everything the program started.
    reason: str
    #: What the framework did about it before this was reported. ``"disposed"`` unless the
    #: host opted down or the disposal did not land — see :data:`DisposalOutcome`.
    disposal: DisposalOutcome = "kept"


async def reclaim_guest_path(
    sandbox: Sandbox, path: str, *, working_directory: str, timeout: float
) -> str | None:
    """Remove ``path`` and everything under it. ``None``, or why it did not happen.

    **No failure of the removal raises.** It runs in a ``finally``, where an exception would
    replace whatever the call was already reporting with a message about cleanup. Cancellation
    is not such a failure: a :class:`~asyncio.CancelledError` or ``GeneratorExit`` at the
    removal is the caller's deadline arriving, and it propagates — containing it would let the
    call return past a bound the host thought it had.

    Dispatches to :meth:`Sandbox.reclaim`, which every backend serves. The guards stay here:
    the backend is the mechanism, not the policy.
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
        # Inside the `try`, so a proxy whose attribute lookup raises is a reason below, not
        # an escape past the caller's `finally`. `getattr` rather than catching the
        # `AttributeError`: a correct `reclaim` raising one of its own is a different fault.
        if not callable(getattr(sandbox, "reclaim", None)):
            return (
                "this backend does not implement `Sandbox.reclaim`, which every backend serves "
                "and no capability gates — every call leaks its directory until it does. "
                "`maf_sandbox.conformance.assert_reclaim_conformance` is what proves an "
                "implementation"
            )
        await sandbox.reclaim(resolved, working_directory=working_directory, timeout=timeout)
    except Exception as refused:  # noqa: BLE001 — an unreclaimed path is a leak, not a fault
        # Only `Exception`. A `CancelledError` here is the caller's own deadline arriving at
        # this await, and answering with a reason would let the call return past it; the caller
        # records the loss and lets it through. See `maf._reclaim_the_call`.
        return f"the removal call failed: {error_detail(refused)}"
    return None
