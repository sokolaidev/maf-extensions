"""Adapter session identities in the existing quota-accounted journal."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from contextlib import nullcontext
from dataclasses import asdict

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


def contract(spec: SandboxSpec) -> str:
    """Bind an admitted contract independently of set and mapping iteration order."""
    fields = asdict(spec)
    fields["requires"] = sorted(spec.requires)
    return identity(json.dumps(fields, sort_keys=True, separators=(",", ":")))


class Catalog:
    """One worker thread owns the root sentinel and all live SQLite connections."""

    def __init__(self, config: MxcSandboxConfig, *, initialize: bool):
        self.config = config
        if initialize:
            config.verify()
        self.verified = initialize
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
        self.maintenance_cursor = ""

    def acquire(self, key: SandboxKey, spec: SandboxSpec) -> tuple[str, bool]:
        """Reopen only the exact admitted execution contract, or allocate a new epoch."""
        if not self.verified:
            self.config.verify()
            self.verified = True
        self.maintain()
        profile = self.profile(key, spec)
        hashed = profile["key"]
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

    def profile(self, key: SandboxKey, spec: SandboxSpec) -> dict[str, str]:
        hashed = identity(key.scope, key.thread_id, key.agent_id, key.call_id, spec.kind)
        return self.config.identity() | {
            "key": hashed,
            "owner": identity(key.scope, key.thread_id, key.agent_id, key.call_id),
            "kind": identity(spec.kind),
            "scope": identity(key.scope, key.thread_id),
            "contract": contract(spec),
        }

    def replay(
        self, key: SandboxKey, spec: SandboxSpec, session: str, call_id: str, request: Request
    ) -> bytes:
        """Read an exact retained result without provisioning or invoking native assets."""
        profile = self.profile(key, spec)

        def read(store: SharedStore) -> bytes:
            encoded = store.db.execute(
                "SELECT profile FROM sessions WHERE id=?", (session,)
            ).fetchone()[0]
            if json.loads(encoded)["profile"] != profile:
                raise Refused("retained session belongs to another execution contract")
            if (
                store.db.execute(
                    "SELECT 1 FROM calls WHERE session=? AND id=?", (session, call_id)
                ).fetchone()
                is None
            ):
                raise Refused("no retained call exists for replay")
            identity = (
                request.identity()
                if self.config.persistent
                else b"mxc-fresh-state-v1\0" + request.identity()
            )
            result = store.begin(call_id, identity)
            if result is None:
                raise Refused("no committed result exists for replay")
            return result

        if session in self.stores:
            return read(self.stores[session])
        with SharedStore(
            self.config.root, session, profile, self.config.limits, existing_only=True
        ) as store:
            return read(store)

    def execute(
        self, session: str, call_id: str, request: Request, cancel: threading.Event, timeout: float
    ) -> bytes:
        """Publish through the native journal before returning a successful result."""

        def complete(result: bytes) -> None:
            streams = json.loads(result)["streams"]
            if any(streams[key]["omitted_bytes"] for key in ("stdout", "stderr")):
                raise Refused("program output exceeds the adapter's independent stream limits")

        store = self.stores[session]
        store.collect_checkpoints()
        store.expire()
        return call(
            store,
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
        store.collect_checkpoints(limit=128)
        store.expire()
        store.close()
        del self.stores[session]

    def maintain(self) -> None:
        """Visit at most eight retired epochs per pass without erasing retry identities."""
        rows = self.sentinel.db.execute(
            "SELECT id,profile FROM sessions WHERE state='retired' AND id>? ORDER BY id LIMIT 8",
            (self.maintenance_cursor,),
        ).fetchall()
        if not rows:
            self.maintenance_cursor = ""
        for row in rows:
            session = row["id"]
            self.maintenance_cursor = session
            if self.sentinel.db.execute(
                "SELECT 1 FROM launches WHERE session=? UNION ALL "
                "SELECT 1 FROM reservations WHERE session=?",
                (session, session),
            ).fetchone():
                continue
            store = self.stores.get(session)
            with (
                nullcontext(store)
                if store is not None
                else SharedStore(
                    self.config.root,
                    session,
                    json.loads(row["profile"])["profile"],
                    self.config.limits,
                    existing_only=True,
                )
            ) as retired:
                if retired.db.execute(
                    "SELECT 1 FROM calls WHERE session=? AND checkpoint_charge>0 LIMIT 1",
                    (session,),
                ).fetchone():
                    retired.collect_checkpoints(limit=128)
                retired.expire()

    def cleanup_sessions(self) -> tuple[tuple[str, dict[str, str]], ...]:
        """Exclude retired delivery history only once native resources and checkpoints are gone."""
        return tuple(
            (row["id"], json.loads(row["profile"])["profile"])
            for row in self.sentinel.db.execute(
                "SELECT id,profile FROM sessions s WHERE id!='mxc_adapter' AND "
                "(state='active' OR EXISTS(SELECT 1 FROM launches WHERE session=s.id) OR "
                "EXISTS(SELECT 1 FROM reservations WHERE session=s.id) OR "
                "EXISTS(SELECT 1 FROM calls WHERE session=s.id AND checkpoint_charge>0))"
            )
        )

    def scope_sessions(self, scope: str, thread_id: str) -> tuple[str, ...]:
        """Include unopened sessions when a host purges a retained conversation."""
        hashed = identity(scope, thread_id)
        return tuple(
            session
            for session, profile in self.cleanup_sessions()
            if profile.get("scope") == hashed
        )

    def key_sessions(self, key: SandboxKey, kind: str | None) -> tuple[str, ...]:
        """Find retained epochs even before this process has acquired a handle."""
        owner = identity(key.scope, key.thread_id, key.agent_id, key.call_id)
        sessions: list[str] = []
        for session, profile in self.cleanup_sessions():
            if profile.get("owner") == owner and (
                kind is None or profile.get("kind") == identity(kind)
            ):
                sessions.append(session)
        return tuple(sessions)

    def close(self) -> None:
        """Release process ownership while leaving persistent checkpoints recoverable."""
        for store in self.stores.values():
            store.close()
        self.stores.clear()
        self.sentinel.close()
