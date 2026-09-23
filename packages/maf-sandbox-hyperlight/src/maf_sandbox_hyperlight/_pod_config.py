"""The immutable ownership binding supplied by the pod controller."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from maf_sandbox import SandboxKey

from ._wire import HyperlightWorkerError

POD_BINDING = "/run/maf-hyperlight/session/binding.json"
POD_SOCKET = "/run/maf-hyperlight/session/control.sock"


@dataclass(frozen=True)
class HyperlightPodConfig:
    """Bind a container to one key/kind; its memory budget covers the entire workload.

    Use ``from_environment`` inside the controller-created pod. This is not a per-worker limit.
    """

    key: SandboxKey
    kind: str
    pod_uid: str
    generation: str
    memory_limit_bytes: int

    def __post_init__(self) -> None:
        for value in (
            self.key.scope,
            self.key.thread_id,
            self.key.agent_id,
            self.kind,
            self.pod_uid,
            self.generation,
        ):
            if (
                not isinstance(cast("object", value), str)
                or not value
                or len(value) > 1024
                or "\x00" in value
            ):
                raise ValueError("pod ownership fields must be nonempty bounded strings")
        if self.key.call_id:
            raise ValueError("the pod integration supports conversation ownership only")
        if type(self.memory_limit_bytes) is not int or self.memory_limit_bytes <= 0:
            raise ValueError("pod memory_limit_bytes must be a positive integer")

    @classmethod
    def from_environment(cls) -> HyperlightPodConfig:
        """Read the supervisor's binding, including the Kubernetes-assigned pod UID."""
        data = json.loads(Path(POD_BINDING).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise HyperlightWorkerError("invalid pod ownership binding")
        fields = cast("dict[str, object]", data)
        if fields.get("owner_pid") != os.getpid():
            raise HyperlightWorkerError("this process does not own the pod")
        return cls.from_mapping(fields)

    @classmethod
    def from_mapping(cls, data: dict[str, object]) -> HyperlightPodConfig:
        """Decode the controller's bounded ownership fields."""
        names = ("scope", "thread_id", "agent_id", "kind", "pod_uid", "generation")
        if any(not isinstance(data.get(name), str) for name in names):
            raise ValueError("invalid pod ownership fields")
        return cls(
            SandboxKey(*(cast("str", data[name]) for name in names[:3])),
            cast("str", data["kind"]),
            cast("str", data["pod_uid"]),
            cast("str", data["generation"]),
            cast("int", data.get("memory_limit_bytes")),
        )

    def mapping(self) -> dict[str, object]:
        """Return the identity bound to controller and local supervisor messages."""
        return {
            "scope": self.key.scope,
            "thread_id": self.key.thread_id,
            "agent_id": self.key.agent_id,
            "kind": self.kind,
            "pod_uid": self.pod_uid,
            "generation": self.generation,
            "memory_limit_bytes": self.memory_limit_bytes,
        }

    def authorize(self, key: SandboxKey, kind: str | None = None) -> None:
        """Reject a caller outside the pod's fixed ownership scope."""
        if key != self.key or (kind is not None and kind != self.kind):
            raise HyperlightWorkerError("the pod belongs to another sandbox ownership scope")
