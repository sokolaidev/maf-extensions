"""What actually held for a sandbox a call was served, as a value a host can persist.

:mod:`maf_sandbox._observer` reports what *happened*, event by event, to a recorder wired for
the whole process.  This module answers the adjacent question — what was live for this call —
as one snapshot written where the conversation already lives, so an incident review reads it
back beside the transcript instead of joining a trace to it.
:func:`~maf_sandbox.maf.effective_state_middleware` is the writer.

**The served answer, not the ask.**  ``backend``, ``isolation`` and the two declaration fields
come from the acquire that succeeded; ``isolation_scope`` is the one the host and the spec
resolved to rather than the one the spec named.  A refused acquire produces no snapshot at all:
nothing held, and the refusal is already an exception, a log line and a
:class:`~maf_sandbox.SandboxAcquired` of its own.

**Posture, never payload.**  Every field here is host configuration or a backend's own
declaration.  No file name, no artifact name, no code and no result text: those are model-chosen
and belong to the transcript rather than to a record of what the sandbox was.

**The spec's ``labels`` are not in it, and that is a decision.**  Labels are host deployment
vocabulary — a tenant, a cost centre, a subscription — and this snapshot is written into session
state, which a host persists and may ship where a transcript does not go.  A recorder that wants
them has :attr:`~maf_sandbox.SandboxAcquired.spec` on the observer seam, where the host chooses
the destination.  The :class:`~maf_sandbox.SandboxKey` is left out for the same reason and one
more: the session is already that conversation, so a scope and a thread id in its own state
answer nothing they do not already.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from ._observer import SandboxAcquired
from ._protocol import (
    Capability,
    Egress,
    Identity,
    Isolation,
    IsolationScope,
    TransferLimits,
)

__all__ = [
    "EffectiveState",
    "close_effective_state_notes",
    "effective_state_is_noted",
    "note_effective_state",
    "open_effective_state_notes",
]


@dataclass(frozen=True)
class EffectiveState:
    """The posture one sandbox was served under, in this package's own vocabulary.

    Built by :meth:`of` from a served :class:`~maf_sandbox.SandboxAcquired`, so the snapshot and
    the event can never disagree about what held.  :meth:`as_dict` is the JSON-native rendering,
    and the only shape a session store is asked to hold.

    ``backend_capabilities`` and ``backend_egress_modes`` are ``None`` together, and only where
    the backend's declarations could not be read back for the record — a property call into
    somebody else's class, which an acquire must not start failing over.  ``None`` there is a
    degraded read of a sandbox that was served, not a backend that declared nothing.
    """

    #: The workload, which is part of the sandbox's identity rather than a display label.
    kind: str
    #: The backend that answered.
    backend: str
    #: The rung it declared, or ``None`` for a degraded read.
    isolation: Isolation | None
    #: How much of the conversation this sandbox serves, as the host and the spec resolved it.
    isolation_scope: IsolationScope
    #: The network posture the sandbox was served under.
    egress: Egress
    #: The hosts an ``ALLOWLIST`` run could reach, and empty in every other mode.
    egress_allow: tuple[str, ...]
    #: What the workload required, which the router matched before it served.
    requires: frozenset[Capability]
    #: The image reference the spec named, unresolved — a backend completes its own.
    image: str | None
    #: The guest directory the workload's paths resolve against.
    work_dir: str
    #: The artifacts the spec declares, by path.  Names chosen at call time are not here: those
    #: are the model's, and this record carries no model-chosen text.
    declared_outputs: tuple[str, ...]
    #: Every tool the sealed host-tool registry was carrying, and empty where none was wired.
    host_tools: frozenset[str]
    #: Whose authority those tools exercise.
    identities: frozenset[Identity]
    #: The workload's own transfer caps, per direction.
    files_in: TransferLimits
    files_out: TransferLimits
    backend_capabilities: frozenset[Capability] | None
    backend_egress_modes: frozenset[Egress] | None

    @classmethod
    def of(cls, event: SandboxAcquired) -> EffectiveState | None:
        """The snapshot for ``event``, or ``None`` where it records no served sandbox.

        ``None`` for a refusal and for an acquire that never chose a backend, which are the two
        cases with no posture to describe.
        """
        if event.refusal is not None or event.backend is None:
            return None
        spec = event.spec
        surface = spec.host_tools
        declarations = event.declarations
        return cls(
            kind=spec.kind,
            backend=event.backend,
            isolation=event.isolation,
            isolation_scope=event.isolation_scope,
            egress=spec.egress,
            egress_allow=tuple(spec.egress_allow),
            requires=frozenset(spec.requires),
            image=spec.image,
            work_dir=spec.work_dir,
            declared_outputs=tuple(declared.path for declared in spec.declared_outputs),
            host_tools=frozenset() if surface is None else frozenset(surface.names),
            identities=frozenset(spec.identities),
            files_in=spec.files_in,
            files_out=spec.files_out,
            backend_capabilities=(
                None if declarations is None else frozenset(declarations.capabilities)
            ),
            backend_egress_modes=(
                None if declarations is None else frozenset(declarations.egress_modes)
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        """This snapshot as JSON-native values, which is what a session store can hold.

        Every key is always present, ``None`` included: a record someone queries a month later
        is easier to read against a fixed shape than against one whose keys come and go.  Sets
        are rendered sorted, so two snapshots of one posture compare equal as JSON.
        """
        return {
            "kind": self.kind,
            "backend": self.backend,
            "isolation": _named(self.isolation),
            "isolation_scope": str(self.isolation_scope),
            "egress": str(self.egress),
            "egress_allow": list(self.egress_allow),
            "requires": _sorted(self.requires),
            "image": self.image,
            "work_dir": self.work_dir,
            "declared_outputs": list(self.declared_outputs),
            "host_tools": _sorted(self.host_tools),
            "identities": _sorted(self.identities),
            "files_in": _caps(self.files_in),
            "files_out": _caps(self.files_out),
            "backend_capabilities": _optional_sorted(self.backend_capabilities),
            "backend_egress_modes": _optional_sorted(self.backend_egress_modes),
        }


def _named(member: object | None) -> str | None:
    return None if member is None else str(member)


def _sorted(members: Iterable[object]) -> list[str]:
    return sorted(str(member) for member in members)


def _optional_sorted(members: Iterable[object] | None) -> list[str] | None:
    return None if members is None else _sorted(members)


def _caps(limits: TransferLimits) -> dict[str, int]:
    return {
        "max_bytes_per_file": limits.max_bytes_per_file,
        "max_total_bytes": limits.max_total_bytes,
        "max_files": limits.max_files,
    }


#: The running call's snapshots, opened by whoever will write them and ``None`` outside one.
_NOTES: ContextVar[list[EffectiveState] | None] = ContextVar(
    "maf_sandbox_effective_state", default=None
)


def open_effective_state_notes() -> tuple[list[EffectiveState], Token[list[EffectiveState] | None]]:
    """Start collecting what a call is served. The caller keeps the list and resets the token."""
    notes: list[EffectiveState] = []
    return notes, _NOTES.set(notes)


def close_effective_state_notes(token: Token[list[EffectiveState] | None]) -> None:
    """Stop collecting: whatever is served from here on belongs to no call."""
    _NOTES.reset(token)


def effective_state_is_noted() -> bool:
    """Whether anything is collecting, so an acquire can skip building a snapshot nobody holds.

    The same promise the observer seam makes: a host that wired no writer pays nothing.
    """
    return _NOTES.get() is not None


def note_effective_state(state: EffectiveState) -> None:
    """Record what one acquire was served, for the call that is collecting.

    A no-op outside one — a router driven directly, or a host that wired no writer. Duplicates
    are dropped rather than appended: a call that acquires twice on one key is served the same
    sandbox, and two identical entries would read as two sandboxes.
    """
    notes = _NOTES.get()
    if notes is not None and state not in notes:
        notes.append(state)
