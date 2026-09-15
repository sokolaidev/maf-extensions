"""Language-neutral records exchanged by the control endpoint and TUI."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import cast


def validate_source_id(value: object) -> str:
    """Return a source identifier that is safe to publish on the control protocol."""
    if not isinstance(value, str) or not value:
        raise ValueError("source_id must be a nonempty string")
    return value


class SandboxState(StrEnum):
    """Lifecycle state visible to an operator."""

    STARTING = "starting"
    READY = "ready"
    RUNNING = "running"
    RESETTING = "resetting"
    DISPOSING = "disposing"
    FAILED = "failed"


@dataclass(frozen=True)
class SandboxRecord:
    """One physical sandbox and the trusted MAF key that owns it.

    ``last_activity_at`` is the last lifecycle signal observed by the monitor, not a guarantee
    that no sandbox execution occurred after that time.
    """

    source_id: str
    backend: str
    scope: str
    thread_id: str
    agent_id: str
    call_id: str
    kind: str
    instance_id: str
    state: SandboxState
    created_at: float
    last_activity_at: float
    process_id: int | None = None
    execution_contract: str | None = None
    egress_targets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_source_id(self.source_id)

    @property
    def logical_name(self) -> str:
        """Compact key for tables and confirmation prompts."""
        call = f"/{self.call_id}" if self.call_id else ""
        return f"{self.scope}/{self.thread_id}/{self.agent_id}{call}"

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON representation used by protocol version one."""
        data = cast("dict[str, object]", asdict(self))
        data["state"] = self.state.value
        data["egress_targets"] = list(self.egress_targets)
        return data

    @classmethod
    def from_json(cls, value: object) -> SandboxRecord:
        """Read a protocol version one sandbox record."""
        if not isinstance(value, dict):
            raise ValueError("sandbox record must be an object")
        data = cast("dict[object, object]", value)

        def text(name: str, *, optional: bool = False) -> str | None:
            item = data.get(name)
            if optional and item is None:
                return None
            if not isinstance(item, str):
                raise ValueError(f"sandbox record {name} must be a string")
            return item

        def number(name: str) -> float:
            item = data.get(name)
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValueError(f"sandbox record {name} must be a number")
            result = float(item)
            if not math.isfinite(result):
                raise ValueError(f"sandbox record {name} must be finite")
            return result

        process_id = data.get("process_id")
        if process_id is not None and (
            isinstance(process_id, bool) or not isinstance(process_id, int)
        ):
            raise ValueError("sandbox record process_id must be an integer or null")
        targets = data.get("egress_targets", [])
        if not isinstance(targets, list):
            raise ValueError("sandbox record egress_targets must be a string array")
        target_items = cast("list[object]", targets)
        if not all(isinstance(item, str) for item in target_items):
            raise ValueError("sandbox record egress_targets must be a string array")
        return cls(
            source_id=cast("str", text("source_id")),
            backend=cast("str", text("backend")),
            scope=cast("str", text("scope")),
            thread_id=cast("str", text("thread_id")),
            agent_id=cast("str", text("agent_id")),
            call_id=cast("str", text("call_id")),
            kind=cast("str", text("kind")),
            instance_id=cast("str", text("instance_id")),
            state=SandboxState(cast("str", text("state"))),
            created_at=number("created_at"),
            last_activity_at=number("last_activity_at"),
            process_id=process_id,
            execution_contract=text("execution_contract", optional=True),
            egress_targets=tuple(cast("str", item) for item in target_items),
        )


class DisposalStatus(StrEnum):
    """Result of an exact-instance disposal request."""

    DISPOSED = "disposed"
    NOT_FOUND = "not_found"
    FAILED = "failed"


@dataclass(frozen=True)
class DisposalResult:
    """Bounded outcome returned to the operator."""

    status: DisposalStatus
    instance_id: str
    message: str

    def to_json(self) -> dict[str, str]:
        """Return the stable JSON representation used by protocol version one."""
        return {
            "status": self.status.value,
            "instance_id": self.instance_id,
            "message": self.message,
        }

    @classmethod
    def from_json(cls, value: object) -> DisposalResult:
        """Read a protocol version one disposal result."""
        if not isinstance(value, dict):
            raise ValueError("disposal result must be an object")
        data = cast("dict[object, object]", value)
        status, instance_id, message = (
            data.get("status"),
            data.get("instance_id"),
            data.get("message"),
        )
        if not all(isinstance(item, str) for item in (status, instance_id, message)):
            raise ValueError("disposal result fields must be strings")
        return cls(
            DisposalStatus(cast("str", status)), cast("str", instance_id), cast("str", message)
        )


class PurgeStatus(StrEnum):
    """Result of a conversation-wide purge request."""

    PURGED = "purged"
    PARTIAL = "partial"


@dataclass(frozen=True)
class PurgeResult:
    """Aggregated outcome of purging one conversation."""

    status: PurgeStatus
    scope: str
    thread_id: str
    disposed: int
    message: str

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON representation used by protocol version one."""
        return {
            "status": self.status.value,
            "scope": self.scope,
            "thread_id": self.thread_id,
            "disposed": self.disposed,
            "message": self.message,
        }

    @classmethod
    def from_json(cls, value: object) -> PurgeResult:
        """Read a protocol version one purge result."""
        if not isinstance(value, dict):
            raise ValueError("purge result must be an object")
        data = cast("dict[object, object]", value)
        status, scope, thread_id, disposed, message = (
            data.get("status"),
            data.get("scope"),
            data.get("thread_id"),
            data.get("disposed"),
            data.get("message"),
        )
        if not all(isinstance(item, str) for item in (status, scope, thread_id, message)):
            raise ValueError("purge result text fields must be strings")
        if isinstance(disposed, bool) or not isinstance(disposed, int) or disposed < 0:
            raise ValueError("purge result disposed must be a nonnegative integer")
        return cls(
            PurgeStatus(cast("str", status)),
            cast("str", scope),
            cast("str", thread_id),
            disposed,
            cast("str", message),
        )
