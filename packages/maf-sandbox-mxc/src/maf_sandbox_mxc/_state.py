"""Adapter session identities in the existing quota-accounted journal."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid

from maf_sandbox import SandboxKey, SandboxSpec

from ._config import MxcSandboxConfig
from .files.request import Request
from .files.shared_call import call
from .host_store import Refused
from .native_journal import NativeJournal
from .shared_store import SharedStore


def identity(*values: str) -> str:
    """Hash framed host identities without storing user-visible names in the journal."""
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


class Catalog:
    """One worker thread owns the root sentinel and all live SQLite connections."""

    def __init__(self, config: MxcSandboxConfig, *, initialize: bool):
        self.config = config
        config.verify()
        if initialize and (config.root / "shared.sqlite").exists():
            raise Refused("initialization requires a new adapter store")
        self.sentinel = SharedStore(
            config.root,
            "mxc_adapter",
            config.identity(),
            config.limits,
            existing_only=not initialize,
        )
        self.stores: dict[str, SharedStore] = {}

    def acquire(self, key: SandboxKey, spec: SandboxSpec) -> tuple[str, bool]:
        """Reopen only the exact admitted execution contract, or allocate a new epoch."""
        hashed = identity(key.scope, key.thread_id, key.agent_id, key.call_id, spec.kind)
        profile = self.config.identity() | {
            "key": hashed,
            "owner": identity(key.scope, key.thread_id, key.agent_id, key.call_id),
            "kind": identity(spec.kind),
            "scope": identity(key.scope, key.thread_id),
            "contract": identity(repr(spec)),
        }
        prefix = "mxc_" + hashed[:32] + "_"
        rows = self.sentinel.db.execute(
            "SELECT id FROM sessions WHERE state='active' AND substr(id,1,?)=?",
            (len(prefix), prefix),
        ).fetchall()
        if len(rows) > 1:
            raise Refused("multiple active adapter epochs require reconciliation")
        session = rows[0]["id"] if rows else prefix + uuid.uuid4().hex
        if session not in self.stores:
            store = SharedStore(
                self.config.root,
                session,
                profile,
                self.config.limits,
                existing_only=bool(rows),
            )
            try:
                journal = NativeJournal(store)
                launches = store.db.execute(
                    "SELECT call FROM launches WHERE session=?",
                    (session,),
                ).fetchall()
                for row in launches:
                    journal.reclaim(row["call"])
            except BaseException:
                store.close()
                raise
            self.stores[session] = store
        elif (
            json.loads(
                self.stores[session]
                .db.execute(
                    "SELECT profile FROM sessions WHERE id=?",
                    (session,),
                )
                .fetchone()[0]
            )["profile"]
            != profile
        ):
            raise Refused("dispose the session before changing its execution contract")
        return session, not rows

    def execute(
        self, session: str, call_id: str, request: Request, cancel: threading.Event, timeout: float
    ) -> bytes:
        """Publish through the native journal before returning a successful result."""

        def complete(result: bytes) -> None:
            streams = json.loads(result)["streams"]
            if any(streams[key]["omitted_bytes"] for key in ("stdout", "stderr")):
                raise Refused("program output exceeds the adapter's independent stream limits")

        return call(
            self.stores[session],
            call_id,
            request,
            self.config.helper,
            self.config.startup,
            self.config.scratch,
            cancel=cancel,
            timeout=timeout,
            restore_state=self.config.persistent,
            validate_result=complete,
        )

    def reclaim(self, session: str) -> None:
        """Release failed-call scratch only after its recorded native owner has stopped."""
        store = self.stores[session]
        rows = store.db.execute("SELECT call FROM launches WHERE session=?", (session,)).fetchall()
        for row in rows:
            NativeJournal(store).reclaim(row["call"])

    def retire(self, session: str) -> None:
        """Retain durable retry identities after retiring the live guest state."""
        store = self.stores.get(session)
        if store is None:
            row = self.sentinel.db.execute(
                "SELECT profile FROM sessions WHERE id=?",
                (session,),
            ).fetchone()
            if row is None:
                return
            profile = json.loads(row["profile"])["profile"]
            store = SharedStore(
                self.config.root,
                session,
                profile,
                self.config.limits,
                existing_only=True,
            )
            self.stores[session] = store
        NativeJournal(store).delete()
        store.close()
        del self.stores[session]

    def scope_sessions(self, scope: str, thread_id: str) -> tuple[str, ...]:
        """Include unopened sessions when a host purges a retained conversation."""
        hashed = identity(scope, thread_id)
        return tuple(
            row["id"]
            for row in self.sentinel.db.execute(
                "SELECT id,profile FROM sessions WHERE state='active' AND id!='mxc_adapter'",
            )
            if json.loads(row["profile"])["profile"].get("scope") == hashed
        )

    def key_sessions(self, key: SandboxKey, kind: str | None) -> tuple[str, ...]:
        """Find retained epochs even before this process has acquired a handle."""
        owner = identity(key.scope, key.thread_id, key.agent_id, key.call_id)
        sessions: list[str] = []
        for row in self.sentinel.db.execute(
            "SELECT id,profile FROM sessions WHERE state='active' AND id!='mxc_adapter'"
        ):
            profile = json.loads(row["profile"])["profile"]
            if profile.get("owner") == owner and (
                kind is None or profile.get("kind") == identity(kind)
            ):
                sessions.append(row["id"])
        return tuple(sessions)

    def close(self) -> None:
        """Release process ownership while leaving persistent checkpoints recoverable."""
        for store in self.stores.values():
            store.close()
        self.stores.clear()
        self.sentinel.close()
