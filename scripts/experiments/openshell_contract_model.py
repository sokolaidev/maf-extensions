"""Executable proposal for #1817/#1818; no OpenShell RPC, persistence or driver implementation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scope:
    """Model server state and authenticated identity, not caller-supplied authorization."""

    epoch: str
    workspace: str
    issuer: str
    subject: str


@dataclass(frozen=True)
class Create:
    """Original create intent bound to its canonical payload digest."""

    scope: Scope
    request_id: str
    name: str
    payload_digest: str


@dataclass(frozen=True)
class Delete:
    """Deletion intent carrying the physical target before admission."""

    scope: Scope
    request_id: str
    name: str
    expected_id: str


@dataclass(frozen=True)
class Receipt:
    """Historical outcome; none of these states proves physical cleanup."""

    state: str
    admission_id: str | None = None
    physical_id: str | None = None


@dataclass
class Admission:
    """Retained original create identity, including an expiry tombstone."""

    intent: Create
    admission_id: str
    physical_id: str | None = None
    expired: bool = False


@dataclass
class Deletion:
    """Replay metadata for a separately retained physical cleanup job."""

    intent: Delete
    job_id: str
    expired: bool = False


class Refused(RuntimeError):
    """A model transition refused before any workload effect."""


class ContractModel:
    """Serialize each transition atomically; separate registry acknowledgement from physical effects."""

    def __init__(self, epoch: str, workspace: str, members: set[tuple[str, str]]) -> None:
        self.epoch, self.workspace = epoch, workspace
        self.members = set(members)
        self.used_epochs = {epoch}
        self.creates: dict[tuple[str, str, str], Admission] = {}
        self.deletes: dict[tuple[str, str, str], Deletion] = {}
        self.jobs: dict[str, Delete] = {}
        self.acknowledged: set[str] = set()
        self.names: dict[str, str] = {}
        self.origins: dict[str, str] = {}
        self.resources: set[str] = set()
        self.effects: list[str] = []
        self.serial = 0

    def _new_id(self, kind: str) -> str:
        self.serial += 1
        return f"{self.epoch}/{kind}/{self.serial}"

    def _status(self, scope: Scope) -> str | None:
        if scope.epoch != self.epoch or scope.workspace != self.workspace:
            return "binding_mismatch"
        if (scope.issuer, scope.subject) not in self.members:
            return "forbidden"
        return None

    def _authorize(self, scope: Scope) -> None:
        if status := self._status(scope):
            raise Refused(status)

    @staticmethod
    def _key(intent: Create | Delete) -> tuple[str, str, str]:
        return intent.scope.issuer, intent.scope.subject, intent.request_id

    def lookup(self, intent: Create) -> Receipt:
        """Read original admission history without starting work or changing any state."""
        if status := self._status(intent.scope):
            return Receipt(status)
        admission = self.creates.get(self._key(intent))
        if admission is None:
            return Receipt("absent_at_observation")
        if admission.intent != intent:
            return Receipt("binding_mismatch")
        if admission.expired:
            return Receipt("history_unavailable")
        if admission.physical_id is None:
            return Receipt("unresolved", admission.admission_id)
        return Receipt("confirmed", admission.admission_id, admission.physical_id)

    def admit_create(self, intent: Create) -> str:
        """Reserve a permanent admission key; expiry cannot admit the same intent anew."""
        self._authorize(intent.scope)
        previous = self.creates.get(self._key(intent))
        if previous is not None:
            if previous.intent != intent or previous.expired:
                raise Refused(
                    "binding_mismatch" if previous.intent != intent else "history_unavailable"
                )
            return previous.admission_id
        admission = Admission(intent, self._new_id("admission"))
        self.creates[self._key(intent)] = admission
        return admission.admission_id

    def complete_create(self, intent: Create, admission_id: str) -> str:
        """Complete only the admitted original, retaining its identity after physical removal."""
        self._authorize(intent.scope)
        admission = self.creates.get(self._key(intent))
        if (
            admission is None
            or admission.intent != intent
            or admission.expired
            or admission.admission_id != admission_id
        ):
            raise Refused("admission_unavailable")
        if admission.physical_id is not None:
            return admission.physical_id
        if intent.name in self.names:
            raise Refused("name_conflict")
        physical_id = self._new_id("sandbox")
        admission.physical_id = physical_id
        self.names[intent.name] = physical_id
        self.origins[physical_id] = intent.name
        self.resources.add(physical_id)
        return physical_id

    def expire_create_receipt(self, intent: Create) -> None:
        """Drop replayability but retain a burned key; unresolved admissions cannot expire."""
        self._authorize(intent.scope)
        admission = self.creates[self._key(intent)]
        if admission.intent != intent or admission.physical_id is None:
            raise Refused("not_confirmed_original")
        admission.expired = True

    def admit_delete(self, intent: Delete) -> str:
        """Compare and capture the expected physical target in one atomic model transition."""
        self._authorize(intent.scope)
        previous = self.deletes.get(self._key(intent))
        if previous is not None:
            if previous.intent != intent or previous.expired:
                raise Refused(
                    "binding_mismatch" if previous.intent != intent else "history_unavailable"
                )
            return previous.job_id
        current = self.names.get(intent.name)
        if self.origins.get(intent.expected_id) != intent.name or (
            current is not None and current != intent.expected_id
        ):
            raise Refused("physical_identity_mismatch")
        job_id = self._new_id("delete")
        self.deletes[self._key(intent)] = Deletion(intent, job_id)
        self.jobs[job_id] = intent
        return job_id

    def _job(self, job_id: str) -> Delete:
        intent = self.jobs.get(job_id)
        if (
            intent is None
            or intent.scope.epoch != self.epoch
            or intent.scope.workspace != self.workspace
        ):
            raise Refused("job_history_unavailable")
        return intent

    def acknowledge_delete(self, job_id: str) -> None:
        """Detach only the original registry entry; acknowledgement alone does not remove resources."""
        intent = self._job(job_id)
        if self.names.get(intent.name) == intent.expected_id:
            del self.names[intent.name]
        self.acknowledged.add(job_id)

    def cleanup_delete(self, job_id: str) -> None:
        """Apply the captured physical target without resolving its name again."""
        intent = self._job(job_id)
        if job_id not in self.acknowledged:
            raise Refused("deletion_not_acknowledged")
        self.resources.discard(intent.expected_id)
        self.effects.append(intent.expected_id)

    def expire_delete_receipt(self, intent: Delete) -> None:
        """Retain the expected target and existing cleanup job when replayability expires."""
        self._authorize(intent.scope)
        deletion = self.deletes[self._key(intent)]
        if deletion.intent != intent:
            raise Refused("binding_mismatch")
        deletion.expired = True

    def lose_history(self, new_epoch: str) -> None:
        """Model externally established state loss; detection and epoch continuity are assumptions."""
        if new_epoch in self.used_epochs:
            raise Refused("epoch_reuse")
        self.epoch = new_epoch
        self.used_epochs.add(new_epoch)
        self.creates.clear()
        self.deletes.clear()
        self.jobs.clear()
        self.acknowledged.clear()
        self.names.clear()
        self.origins.clear()
