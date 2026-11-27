"""Host-selected routing to one preprovisioned, generation-bound coordinator session."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from maf_sandbox import SandboxKey, SandboxSpec

from . import remote_scratch
from ._config import MxcSandboxConfig
from ._state import complete_result, contract, identity
from .files import transport
from .files.request import Request
from .files.shared_call import check_file_budget
from .host_store import Refused
from .native_journal import NativeJournal
from .remote_api import Client, Session
from .remote_authority import Authority
from .remote_host import Host
from .shared_store import ScratchLimits


def _routing_profile(
    config: MxcSandboxConfig, key: SandboxKey, spec: SandboxSpec
) -> dict[str, str]:
    return config.identity() | {
        "policy": "closed",
        "key": identity(key.scope, key.thread_id, key.agent_id, key.call_id, spec.kind),
        "contract": contract(spec),
    }


@dataclass(frozen=True)
class MxcCoordinatorBinding:
    """Attach a worker's TLS client and provisioned generation to one admitted router key."""

    client: Client
    host: Host
    key: SandboxKey
    spec: SandboxSpec
    compatibility: Mapping[str, str]

    @staticmethod
    def session(
        config: MxcSandboxConfig,
        key: SandboxKey,
        spec: SandboxSpec,
        compatibility: Mapping[str, str],
    ) -> Session:
        """Construct the immutable coordinator profile before acquiring an owner generation."""
        if not config.persistent:
            raise ValueError("coordinator recovery requires persistent sessions")
        if not all(compatibility.get(name) for name in ("platform", "architecture", "cpu_vendor")):
            raise ValueError("host platform, architecture and CPU vendor are required")
        return Session.for_files(
            {**compatibility, **_routing_profile(config, key, spec)}, config.limits, config.files
        )


class RemoteCatalog:
    """Serve one authorized epoch through the coordinator and its local generation journal."""

    def __init__(self, config: MxcSandboxConfig, binding: MxcCoordinatorBinding):
        host = binding.host
        session = binding.session(config, binding.key, binding.spec, binding.compatibility)
        if (
            host.owner is None
            or host.owner.profile != session.digest
            or host.config != session
            or host.root != config.root
            or config.scratch
            != ScratchLimits(host.scratch.bytes, host.scratch.entries, host.scratch.total_bytes)
        ):
            raise Refused("coordinator generation differs from the configured adapter")
        self.config, self.binding = config, binding
        self.session = host.session
        self.owner = Authority._encode(host.owner)  # pyright: ignore[reportPrivateUsage]
        self.opened = False

    def _status(self) -> dict[str, object]:
        status = self.binding.client.call("status", self.session, owner=self.owner)
        if status.get("scratch") != asdict(self.binding.host.scratch):
            raise Refused("coordinator scratch policy differs from the execution host")
        return status

    def _check_scope(self, key: SandboxKey, spec: SandboxSpec) -> None:
        if key != self.binding.key or spec != self.binding.spec:
            raise Refused("coordinator session belongs to a different key or execution contract")

    def acquire(self, key: SandboxKey, spec: SandboxSpec) -> tuple[str, bool]:
        self._check_scope(key, spec)
        state = self._status()
        if state["active"] is not True:
            raise Refused("coordinator session is retired")
        if not self.opened:
            self.config.verify()
            self.binding.host.verify_provisioned()
        fresh = not self.opened and state["checkpoint"] is None
        self.opened = True
        return self.session, fresh

    def _check_session(self, session: str) -> None:
        if session != self.session:
            raise Refused("coordinator session identity differs")

    @staticmethod
    def _request(request: Request) -> bytes:
        return b"mxc-router-file-v1\0" + hashlib.sha256(request.identity()).digest()

    def replay(
        self, key: SandboxKey, spec: SandboxSpec, session: str, call_id: str, request: Request
    ) -> bytes:
        self._check_scope(key, spec)
        self._check_session(session)
        result = self.binding.client.call_result(
            "retry",
            session,
            self.owner,
            call_id,
            self._request(request),
            result_bytes=self.binding.host.config.limits.result_bytes,
        )["result"]
        if not isinstance(result, bytes):
            raise Refused("missing retained file result")
        return result

    def execute(
        self, session: str, call_id: str, request: Request, cancel: threading.Event, timeout: float
    ) -> bytes:
        self._check_session(session)
        host, client = self.binding.host, self.binding.client
        limits = host.config.limits
        deadline = time.monotonic() + timeout

        def active() -> None:
            if cancel.is_set() or time.monotonic() >= deadline:
                raise Refused("file call cancelled or timed out")

        active()
        check_file_budget(request, limits, self.config.scratch)
        token = self._request(request)
        admitted = client.call_result(
            "begin", session, self.owner, call_id, token, result_bytes=limits.result_bytes
        )
        if isinstance(admitted["result"], bytes):
            return admitted["result"]
        receipt = remote_scratch.Receipt(**admitted["scratch"])
        if receipt.call != call_id or receipt.owner != self.owner:
            raise Refused("coordinator scratch admission differs")

        def run(base: Path, work: Path, restoring: bool, journal: NativeJournal) -> bytes:
            active()
            try:
                result = transport.execute(
                    self.config.helper,
                    base,
                    work,
                    request,
                    restoring=restoring,
                    before_spawn=lambda: journal.start(receipt.token),
                    before_start=lambda child: journal.arm(receipt.token, child),
                    check_active=active,
                    cancel=cancel,
                    timeout=deadline - time.monotonic(),
                    checkpoint_limits=(limits.checkpoint_bytes, limits.files),
                )
            except transport.GuestProgramError as error:
                complete_result(error.result)
                raise
            active()
            complete_result(result)
            return result

        return host.execute_reserved(client, receipt, token, self.config.startup, run)

    def reclaim(self, session: str) -> None:
        self._check_session(session)
        if self._status()["pending"]:
            raise Refused("remote scratch requires trusted reconciliation")

    def retire(self, session: str) -> None:
        self._check_session(session)
        if self.binding.client.call("retire", session, owner=self.owner) != {"retired": True}:
            raise Refused("coordinator did not confirm retirement")

    def scope_sessions(self, scope: str, thread_id: str) -> tuple[str, ...]:
        key = self.binding.key
        if (scope, thread_id) == (key.scope, key.thread_id) and self._status()["active"]:
            return (self.session,)
        return ()

    def key_sessions(self, key: SandboxKey, kind: str | None) -> tuple[str, ...]:
        if (
            key == self.binding.key
            and kind in (None, self.binding.spec.kind)
            and self._status()["active"]
        ):
            return (self.session,)
        return ()

    def close(self) -> None:
        self.opened = False
