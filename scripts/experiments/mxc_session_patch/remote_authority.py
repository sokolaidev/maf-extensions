"""Experimental coordinator ownership journal; no network or native execution adapter."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .host_store import Refused, _lock, _name
from .private_root import check_file, prepare

MAX_GENERATION = 2**63 - 1
MAX_DATABASE_BYTES = 32 * 1024**2


@dataclass(frozen=True)
class Resource:
    """One guest incarnation on one execution-machine incarnation."""

    host: str
    boot: str
    guest: str

    def __post_init__(self) -> None:
        for value in (self.host, self.boot, self.guest):
            _name(value)


@dataclass(frozen=True)
class Owner:
    """Opaque generation-bound authority, usable only through the trusted coordinator."""

    session: str
    generation: int
    profile: str
    resource: Resource
    token: str


@dataclass(frozen=True)
class Fence:
    """A durable termination request; its existence is not termination evidence."""

    id: str
    resource: Resource
    scope: str
    automatic: bool
    sessions: tuple[str, ...]


@dataclass(frozen=True)
class Policy:
    """Trusted shutdown configuration, separate from enabling remote ownership."""

    automatic_shutdown: bool = False
    machine_shutdown_hosts: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if type(self.automatic_shutdown) is not bool:
            raise Refused("automatic shutdown requires an explicit boolean")
        for host in self.machine_shutdown_hosts:
            _name(host)


class Authority:
    """Serialize trusted ownership operations on coordinator-local SQLite.

    Callers supply authenticated host identities and verified compatibility digests.
    This journal does not authenticate callers, stop resources or publish checkpoints.
    """

    def __init__(
        self,
        root: Path,
        *,
        enabled: bool = False,
        policy: Policy = Policy(),
        capacity: int = 4096,
    ):
        if enabled is not True:
            raise Refused("remote coordinator requires explicit opt-in")
        if type(capacity) is not int or not 1 <= capacity <= 100_000:
            raise Refused("invalid coordinator metadata capacity")
        self.policy = policy
        self.capacity = capacity
        prepare(root)
        self.root = root.resolve()
        if any((self.root / name).exists() for name in ("shared.sqlite", "state.sqlite")):
            raise Refused("local session store requires explicit migration")
        check_file(self.root / "initialize.lock")
        with (self.root / "initialize.lock").open("a+b") as initializer:
            if initializer.seek(0, 2) == 0:
                initializer.write(b"0")
                initializer.flush()
            _lock(initializer, True)
            path = self.root / "authority.sqlite"
            for suffix in ("", "-journal", "-wal", "-shm"):
                check_file(Path(str(path) + suffix))
            existed = path.exists()
            self.db = sqlite3.connect(path, timeout=0, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            try:
                self.db.execute("PRAGMA journal_mode=DELETE")
                self.db.execute("PRAGMA synchronous=FULL")
                self.db.execute("PRAGMA foreign_keys=ON")
                page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
                pages = MAX_DATABASE_BYTES // page_size
                if self.db.execute("PRAGMA page_count").fetchone()[0] > pages:
                    raise Refused("coordinator database exceeds its experimental ceiling")
                if self.db.execute(f"PRAGMA max_page_count={pages}").fetchone()[0] != pages:
                    raise Refused("coordinator database ceiling unavailable")
                with self._transaction():
                    if not existed:
                        self.db.execute(
                            "CREATE TABLE settings(version INTEGER NOT NULL, capacity INTEGER NOT NULL)"
                        )
                        self.db.execute("INSERT INTO settings VALUES(1,?)", (capacity,))
                        self.db.execute(
                            "CREATE TABLE sessions(id TEXT PRIMARY KEY, profile TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>0), state TEXT NOT NULL CHECK(state IN ('active','fencing','stopped')), owner TEXT NOT NULL, fence TEXT)"
                        )
                        self.db.execute(
                            "CREATE TABLE resources(host TEXT NOT NULL, boot TEXT NOT NULL, guest TEXT NOT NULL, PRIMARY KEY(host,boot,guest))"
                        )
                        self.db.execute(
                            "CREATE TABLE fences(id TEXT PRIMARY KEY, request TEXT NOT NULL, owner TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','confirmed')))"
                        )
                    settings = self.db.execute("SELECT version,capacity FROM settings").fetchall()
                    if [tuple(row) for row in settings] != [(1, capacity)]:
                        raise Refused("coordinator format or capacity differs")
                    if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise Refused("coordinator integrity check failed")
            except BaseException:
                self.db.close()
                raise

    def __enter__(self) -> Authority:
        return self

    def __exit__(self, *_: object) -> None:
        self.db.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        try:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        except sqlite3.Error as error:
            raise Refused("coordinator storage unavailable or corrupt") from error

    @staticmethod
    def _encode(value: Owner | Fence) -> str:
        record = asdict(value)
        digest = hashlib.sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return json.dumps(
            {"record": record, "sha256": digest}, sort_keys=True, separators=(",", ":")
        )

    @staticmethod
    def _record(value: str) -> dict:
        envelope = json.loads(value)
        if not isinstance(envelope, dict) or set(envelope) != {"record", "sha256"}:
            raise Refused("corrupt coordinator metadata envelope")
        record = envelope["record"]
        digest = hashlib.sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if not isinstance(record, dict) or envelope["sha256"] != digest:
            raise Refused("corrupt coordinator metadata digest")
        return record

    @staticmethod
    def _decode_owner(value: str) -> Owner:
        try:
            data = Authority._record(value)
            resource = Resource(**data.pop("resource"))
            owner = Owner(resource=resource, **data)
            _name(owner.session)
            if type(owner.generation) is not int or not 1 <= owner.generation <= MAX_GENERATION:
                raise ValueError("invalid generation")
            if not re.fullmatch(r"[0-9a-f]{64}", owner.profile):
                raise ValueError("invalid compatibility identity")
            if not re.fullmatch(r"[0-9a-f]{32}", owner.token):
                raise ValueError("invalid token")
            return owner
        except (TypeError, ValueError, KeyError, AttributeError) as error:
            raise Refused("corrupt owner metadata") from error

    @staticmethod
    def _decode_fence(value: str) -> Fence:
        try:
            data = Authority._record(value)
            resource = Resource(**data.pop("resource"))
            members = tuple(data.pop("sessions"))
            fence = Fence(resource=resource, sessions=members, **data)
            _name(fence.id)
            if fence.scope not in ("guest", "machine") or type(fence.automatic) is not bool:
                raise ValueError("invalid fence policy")
            if not members or tuple(sorted(set(members))) != members:
                raise ValueError("invalid fence members")
            for session in members:
                _name(session)
            return fence
        except (TypeError, ValueError, KeyError, AttributeError) as error:
            raise Refused("corrupt termination metadata") from error

    def _space(self, table: str) -> None:
        # Identifiers are internal constants, never transport input.
        if table not in ("sessions", "resources", "fences"):
            raise Refused("unknown coordinator ledger")
        if self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] >= self.capacity:
            raise Refused("coordinator metadata capacity exhausted")

    def _owner(self, owner: Owner) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM sessions WHERE id=?", (owner.session,)).fetchone()
        if (
            row is None
            or row["state"] != "active"
            or row["generation"] != owner.generation
            or row["profile"] != owner.profile
            or self._decode_owner(row["owner"]) != owner
        ):
            raise Refused("stale or revoked owner")
        return row

    def _stopped(self, row: sqlite3.Row) -> None:
        previous = self._decode_owner(row["owner"])
        recorded = self.db.execute("SELECT * FROM fences WHERE id=?", (row["fence"],)).fetchone()
        if recorded is None or recorded["state"] != "confirmed":
            raise Refused("stopped session lacks confirmed termination")
        fence = self._decode_fence(recorded["request"])
        covered = previous.resource == fence.resource or (
            fence.scope == "machine"
            and (previous.resource.host, previous.resource.boot)
            == (fence.resource.host, fence.resource.boot)
        )
        if (
            not covered
            or fence.id != row["fence"]
            or row["id"] not in fence.sessions
            or previous.session != row["id"]
            or previous.profile != row["profile"]
            or previous.generation + 1 != row["generation"]
        ):
            raise Refused("stopped session termination binding differs")

    def acquire(self, session: str, profile: str, resource: Resource) -> Owner:
        """Create or reacquire only a stopped session, preserving its compatibility identity."""
        _name(session)
        if not re.fullmatch(r"[0-9a-f]{64}", profile):
            raise Refused("compatibility identity must be a SHA-256 digest")
        with self._transaction():
            row = self.db.execute("SELECT * FROM sessions WHERE id=?", (session,)).fetchone()
            if row is not None:
                if row["profile"] != profile:
                    raise Refused("runtime or policy compatibility differs")
                if row["state"] == "active":
                    owner = self._decode_owner(row["owner"])
                    self._owner(owner)
                    if owner.resource == resource:
                        return owner
                    raise Refused("session already has an owner")
                if row["state"] != "stopped":
                    raise Refused("termination is not confirmed")
                self._stopped(row)
                if row["generation"] >= MAX_GENERATION:
                    raise Refused("owner generation exhausted")
                generation = row["generation"] + 1
            else:
                self._space("sessions")
                generation = 1
            for stored in self.db.execute("SELECT request FROM fences"):
                fence = self._decode_fence(stored[0])
                if (fence.resource.host, fence.resource.boot) == (resource.host, resource.boot):
                    if fence.scope == "machine" or fence.resource.guest == resource.guest:
                        raise Refused("execution resource is fenced")
            if self.db.execute(
                "SELECT 1 FROM resources WHERE host=? AND boot=? AND guest=?",
                (resource.host, resource.boot, resource.guest),
            ).fetchone():
                raise Refused("execution resource incarnation was already used")
            self._space("resources")
            owner = Owner(session, generation, profile, resource, uuid.uuid4().hex)
            self.db.execute(
                "INSERT INTO resources VALUES(?,?,?)",
                (resource.host, resource.boot, resource.guest),
            )
            if row is None:
                self.db.execute(
                    "INSERT INTO sessions VALUES(?,?,?,'active',?,NULL)",
                    (session, profile, generation, self._encode(owner)),
                )
            else:
                self.db.execute(
                    "UPDATE sessions SET generation=?,state='active',owner=?,fence=NULL WHERE id=?",
                    (generation, self._encode(owner), session),
                )
            return owner

    @contextmanager
    def guard(self, owner: Owner) -> Iterator[sqlite3.Connection]:
        """Keep authority unchanged during a short trusted publication transaction.

        Payload transfer and execution must happen outside this transaction. Publication
        rows in this connection commit atomically with the generation check.
        """
        with self._transaction():
            self._owner(owner)
            yield self.db

    def _authorize_shutdown(self, fence: Fence) -> None:
        if fence.automatic and not self.policy.automatic_shutdown:
            raise Refused("automatic shutdown is disabled")
        if (
            fence.scope == "machine"
            and fence.resource.host not in self.policy.machine_shutdown_hosts
        ):
            raise Refused("whole-machine shutdown is not authorized")

    def request_fence(
        self, owner: Owner, request_id: str, *, scope: str = "guest", automatic: bool = False
    ) -> Fence:
        """Revoke affected owners before returning a pending termination request."""
        _name(request_id)
        if scope not in ("guest", "machine") or type(automatic) is not bool:
            raise Refused("invalid termination scope or mode")
        with self._transaction():
            previous = self.db.execute("SELECT * FROM fences WHERE id=?", (request_id,)).fetchone()
            if previous is not None:
                fence = self._decode_fence(previous["request"])
                if previous["owner"] != self._encode(owner) or (fence.scope, fence.automatic) != (
                    scope,
                    automatic,
                ):
                    raise Refused("termination request identity differs")
                self._authorize_shutdown(fence)
                return fence
            self._owner(owner)
            members = []
            for row in self.db.execute("SELECT * FROM sessions ORDER BY id"):
                current = self._decode_owner(row["owner"])
                affected = current.resource == owner.resource or (
                    scope == "machine"
                    and (current.resource.host, current.resource.boot)
                    == (owner.resource.host, owner.resource.boot)
                )
                if affected and row["state"] != "stopped":
                    if row["state"] != "active":
                        raise Refused("overlapping termination is pending")
                    if row["generation"] >= MAX_GENERATION:
                        raise Refused("owner generation exhausted")
                    members.append(row["id"])
            fence = Fence(request_id, owner.resource, scope, automatic, tuple(members))
            self._authorize_shutdown(fence)
            self._space("fences")
            self.db.execute(
                "INSERT INTO fences VALUES(?,?,?,'pending')",
                (request_id, self._encode(fence), self._encode(owner)),
            )
            for session in members:
                self.db.execute(
                    "UPDATE sessions SET generation=generation+1,state='fencing',fence=? WHERE id=?",
                    (request_id, session),
                )
            return fence

    def confirm_fence(self, request_id: str, verify: Callable[[Fence], bool]) -> None:
        """Require trusted verification of termination and denial of stale-resource restart.

        The verifier is coordinator configuration, never a guest or worker-supplied claim.
        It must bind its evidence to the complete request, including its incarnation.
        """
        _name(request_id)
        with self._transaction():
            row = self.db.execute("SELECT * FROM fences WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise Refused("unknown termination request")
            if row["state"] == "confirmed":
                return
            fence = self._decode_fence(row["request"])
        if verify(fence) is not True:
            raise Refused("termination and stale-restart prevention are not confirmed")
        with self._transaction():
            row = self.db.execute("SELECT * FROM fences WHERE id=?", (request_id,)).fetchone()
            if row is None or self._decode_fence(row["request"]) != fence:
                raise Refused("termination request changed")
            if row["state"] == "confirmed":
                return
            rows = self.db.execute(
                "SELECT id,state FROM sessions WHERE fence=? ORDER BY id", (request_id,)
            ).fetchall()
            if tuple(row["id"] for row in rows) != fence.sessions or any(
                row["state"] != "fencing" for row in rows
            ):
                raise Refused("termination membership changed")
            self.db.execute("UPDATE sessions SET state='stopped' WHERE fence=?", (request_id,))
            self.db.execute("UPDATE fences SET state='confirmed' WHERE id=?", (request_id,))
