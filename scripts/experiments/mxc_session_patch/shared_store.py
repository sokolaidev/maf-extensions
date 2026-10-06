"""Experimental shared SQLite publication; with optional journaled native execution."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat
import time
import uuid
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .host_store import (
    CHUNK,
    MAX_CHECKPOINT,
    MAX_FILES,
    MAX_RESULT,
    Refused,
    _lock,
    _name,
    _relative,
)
from .private_root import check_file, prepare

VERSION = 3
STORE_METADATA = 4096
SESSION_METADATA = 20480
CALL_METADATA = 2048
PATH_BYTES = 512
FILE_METADATA = PATH_BYTES + 256
REFERENCE_METADATA = 80
SECOND = 1_000_000_000
MAX_INTEGER = 2**63 - 1


@dataclass(frozen=True)
class Limits:
    """Explicit logical quotas and bounded publication policy for one session."""

    session_quota: int
    store_quota: int
    checkpoint_bytes: int = MAX_CHECKPOINT
    result_bytes: int = MAX_RESULT
    files: int = MAX_FILES
    retention_seconds: int = 86400
    grace_seconds: int = 300

    def __post_init__(self) -> None:
        for value in (
            self.session_quota,
            self.store_quota,
            self.checkpoint_bytes,
            self.result_bytes,
            self.files,
            self.retention_seconds,
        ):
            if type(value) is not int or not 0 < value <= MAX_INTEGER:
                raise Refused("limits must be positive bounded integers")
        if type(self.grace_seconds) is not int or not 0 <= self.grace_seconds <= MAX_INTEGER:
            raise Refused("invalid grace allowance")
        if (
            self.checkpoint_bytes > MAX_CHECKPOINT
            or self.result_bytes > MAX_RESULT
            or self.files > MAX_FILES
        ):
            raise Refused("publication limits exceed the experimental format")
        if (self.retention_seconds + self.grace_seconds) * SECOND > MAX_INTEGER:
            raise Refused("retention duration exceeds timestamp range")

    @property
    def reservation(self) -> int:
        """Maximum payload and inventory charge, excluding the permanent call identity."""
        references = (self.checkpoint_bytes + CHUNK - 1) // CHUNK + self.files
        return (
            self.checkpoint_bytes
            + self.result_bytes
            + self.files * FILE_METADATA
            + references * REFERENCE_METADATA
        )


@dataclass(frozen=True)
class ScratchLimits:
    """Explicit temporary bytes, filesystem entries and shared temporary-space budget."""

    bytes: int
    entries: int
    store_bytes: int

    def __post_init__(self) -> None:
        if (
            any(
                type(v) is not int or not 0 < v <= MAX_INTEGER
                for v in (self.bytes, self.entries, self.store_bytes)
            )
            or self.bytes > self.store_bytes
        ):
            raise Refused("invalid scratch allowance")


class Clock(Protocol):
    """Paired host UTC and elapsed-time observations."""

    def utc_ns(self) -> int: ...
    def monotonic_ns(self) -> int: ...


class SystemClock:
    """Use host UTC and Python's monotonic clock."""

    def utc_ns(self) -> int:
        return time.time_ns()

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


class SharedStore:
    """Own one session in a shared local store; interrupted reservations require reconciliation."""

    def __init__(
        self,
        root: Path,
        session: str,
        profile: dict[str, str],
        limits: Limits,
        clock: Clock | None = None,
    ):
        if (
            limits.session_quota < SESSION_METADATA
            or limits.store_quota < STORE_METADATA + SESSION_METADATA
        ):
            raise Refused("quota cannot admit session metadata")
        self.session = _name(session)
        self.limits = limits
        self.clock = clock or SystemClock()
        self.grants: dict[str, int] = {}
        self.uncertain = False
        self.anchor_before = self.clock.monotonic_ns()
        self.anchor_utc = self.clock.utc_ns()
        self.anchor_after = self.clock.monotonic_ns()
        self.last_utc = self.anchor_utc
        self._timestamp(self.anchor_utc)
        encoded = json.dumps(
            {
                "profile": profile,
                "checkpoint_bytes": limits.checkpoint_bytes,
                "result_bytes": limits.result_bytes,
                "files": limits.files,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded.encode()) > SESSION_METADATA - 4096:
            raise Refused("profile exceeds metadata allowance")
        prepare(root)
        self.root = root.resolve()
        self.db: sqlite3.Connection
        self.lock = None
        check_file(self.root / "initialize.lock")
        with (self.root / "initialize.lock").open("a+b") as initializer:
            if initializer.seek(0, 2) == 0:
                initializer.write(b"0")
                initializer.flush()
            _lock(initializer, True)
            path = self.root / "shared.sqlite"
            for suffix in ("", "-journal", "-wal", "-shm"):
                check_file(self.root / ("shared.sqlite" + suffix))
            existed = path.exists()
            if (self.root / "state.sqlite").exists() and not existed:
                raise Refused("legacy store requires explicit migration")
            self.db = sqlite3.connect(path, timeout=0, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            try:
                if existed:
                    try:
                        row = self.db.execute("SELECT version FROM settings WHERE id=1").fetchone()
                    except sqlite3.DatabaseError as error:
                        raise Refused("unsupported or corrupt store format") from error
                    if row is None or row[0] != VERSION:
                        raise Refused("unsupported or corrupt store format")
                self.db.execute("PRAGMA journal_mode=DELETE")
                self.db.execute("PRAGMA synchronous=FULL")
                self.db.execute("PRAGMA foreign_keys=ON")
                with self._transaction():
                    if not existed:
                        self._schema()
                        self.db.execute(
                            "INSERT INTO settings VALUES(1, ?, ?)", (VERSION, limits.store_quota)
                        )
                    self.db.execute(
                        "CREATE INDEX IF NOT EXISTS calls_expiry ON calls(session,status,expired,expires,id)"
                    )
                    self.db.execute(
                        "CREATE INDEX IF NOT EXISTS calls_uncertain ON calls(session,status,expired,uncertain)"
                    )
                    if (
                        self.db.execute("SELECT quota FROM settings").fetchone()[0]
                        != limits.store_quota
                    ):
                        raise Refused("shared-store quota configuration differs")
                    row = self.db.execute(
                        "SELECT * FROM sessions WHERE id=?", (session,)
                    ).fetchone()
                    if row is None:
                        if (
                            SESSION_METADATA > limits.session_quota
                            or self.usage() + SESSION_METADATA > limits.store_quota
                        ):
                            raise Refused("quota cannot admit session metadata")
                        self.db.execute(
                            "INSERT INTO sessions VALUES(?, ?, 0, 'active', NULL, ?)",
                            (session, encoded, limits.session_quota),
                        )
                    elif row["profile"] != encoded or row["quota"] != limits.session_quota:
                        raise Refused("session profile or quota configuration differs")
            except BaseException:
                self.db.close()
                raise
        try:
            lock_path = self.root / (hashlib.sha256(session.encode()).hexdigest() + ".lock")
            check_file(lock_path)
            self.lock = lock_path.open("a+b")
            if self.lock.seek(0, 2) == 0:
                self.lock.write(b"0")
                self.lock.flush()
            try:
                _lock(self.lock, True)
            except OSError as error:
                raise Refused("session already has an owner") from error
            with self._transaction():
                row = self.db.execute(
                    "SELECT generation FROM sessions WHERE id=?", (session,)
                ).fetchone()
                if row[0] >= MAX_INTEGER:
                    raise Refused("owner generation exhausted")
                self.generation = row[0] + 1
                self.db.execute(
                    "UPDATE sessions SET generation=? WHERE id=?", (self.generation, session)
                )
                self.db.execute(
                    "UPDATE calls SET status='interrupted' WHERE session=? AND status='pending'",
                    (session,),
                )
                self.db.execute(
                    "UPDATE calls SET uncertain=1 WHERE session=? AND status='committed' AND expired=0 AND uncertain=0",
                    (session,),
                )
        except BaseException:
            self.close()
            raise

    def _schema(self) -> None:
        for statement in (
            "CREATE TABLE settings(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, quota INTEGER NOT NULL CHECK(quota>0))",
            "CREATE TABLE sessions(id TEXT PRIMARY KEY, profile TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>=0), state TEXT NOT NULL CHECK(state IN ('active','retired')), current_call TEXT, quota INTEGER NOT NULL CHECK(quota>0))",
            "CREATE TABLE calls(session TEXT NOT NULL REFERENCES sessions(id), id TEXT NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','interrupted','committed')), generation INTEGER NOT NULL CHECK(generation>=0), result BLOB, result_hash TEXT, result_charge INTEGER NOT NULL DEFAULT 0 CHECK(result_charge>=0), checkpoint_charge INTEGER NOT NULL DEFAULT 0 CHECK(checkpoint_charge>=0), checkpoint_hash TEXT, expires INTEGER CHECK(expires>=0), grace INTEGER CHECK(grace>=0), remaining INTEGER CHECK(remaining>=0 AND remaining<=grace), uncertain INTEGER NOT NULL DEFAULT 0 CHECK(uncertain IN (0,1)), expired INTEGER NOT NULL DEFAULT 0 CHECK(expired IN (0,1)), PRIMARY KEY(session,id))",
            "CREATE TABLE reservations(session TEXT NOT NULL, call TEXT NOT NULL, generation INTEGER NOT NULL, charge INTEGER NOT NULL CHECK(charge>=0), PRIMARY KEY(session,call), FOREIGN KEY(session,call) REFERENCES calls(session,id))",
            "CREATE TABLE files(session TEXT NOT NULL, call TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL CHECK(size>=0), hash TEXT NOT NULL, PRIMARY KEY(session,call,path), FOREIGN KEY(session,call) REFERENCES calls(session,id))",
            "CREATE TABLE chunks(hash TEXT PRIMARY KEY, data BLOB NOT NULL)",
            "CREATE TABLE file_chunks(session TEXT NOT NULL, call TEXT NOT NULL, path TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal>=0), hash TEXT NOT NULL REFERENCES chunks(hash), PRIMARY KEY(session,call,path,ordinal), FOREIGN KEY(session,call,path) REFERENCES files(session,call,path))",
            "CREATE INDEX file_chunks_hash ON file_chunks(hash)",
            "CREATE TABLE scratch_settings(id INTEGER PRIMARY KEY CHECK(id=1), quota INTEGER NOT NULL CHECK(quota>0))",
            "CREATE TABLE launches(session TEXT NOT NULL, call TEXT NOT NULL, token TEXT NOT NULL UNIQUE, charge INTEGER NOT NULL CHECK(charge>0), entries INTEGER NOT NULL CHECK(entries>0), identity TEXT, state TEXT NOT NULL CHECK(state IN ('prepared','armed','cleaning')), PRIMARY KEY(session,call), FOREIGN KEY(session,call) REFERENCES calls(session,id))",
        ):
            self.db.execute(statement)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        grants = self.grants.copy()
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            self.grants = grants
            raise

    def __enter__(self) -> SharedStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Release ownership without refunding reservations or issued grace."""
        self.db.close()
        if self.lock is not None:
            self.lock.close()
        self.grants.clear()

    def _owner(self) -> sqlite3.Row:
        if self.lock is None or self.lock.closed:
            raise Refused("owner is closed")
        row = self.db.execute("SELECT * FROM sessions WHERE id=?", (self.session,)).fetchone()
        if row is None or row["generation"] != self.generation:
            raise Refused("owner generation changed")
        return row

    def _timestamp(self, value: int) -> None:
        if (
            type(value) is not int
            or not 0
            <= value
            <= MAX_INTEGER - (self.limits.retention_seconds + self.limits.grace_seconds) * SECOND
        ):
            raise Refused("host timestamp outside supported range")

    def _observe(self) -> tuple[int, int]:
        before = self.clock.monotonic_ns()
        utc = self.clock.utc_ns()
        after = self.clock.monotonic_ns()
        self._timestamp(utc)
        lower = self.anchor_utc + before - self.anchor_after
        upper = self.anchor_utc + after - self.anchor_before
        if utc < self.last_utc or utc < lower - SECOND or utc > upper + SECOND:
            self.uncertain = True
        if self.uncertain:
            self.db.execute(
                "UPDATE calls SET uncertain=1 WHERE session=? AND status='committed' AND expired=0 AND uncertain=0",
                (self.session,),
            )
        self.last_utc = utc
        return utc, before

    def usage(self, session: str | None = None) -> int:
        """Return retained logical charges plus durable reservations."""
        where = "" if session is None else " WHERE session=?"
        args = () if session is None else (session,)
        count = self.db.execute(
            "SELECT count(*) FROM sessions" + ("" if session is None else " WHERE id=?"), args
        ).fetchone()[0]
        retained = self.db.execute(
            "SELECT coalesce(sum(? + result_charge + checkpoint_charge),0) FROM calls" + where,
            (CALL_METADATA, *args),
        ).fetchone()[0]
        reserved = self.db.execute(
            "SELECT coalesce(sum(charge),0) FROM reservations" + where, args
        ).fetchone()[0]
        return (
            (STORE_METADATA if session is None else 0)
            + count * SESSION_METADATA
            + retained
            + reserved
        )

    def _available(self, row: sqlite3.Row, utc: int, monotonic: int) -> bool:
        if row["expired"]:
            return False
        if utc < row["expires"]:
            self.grants.pop(row["id"], None)
            return True
        if not row["uncertain"] or utc >= row["expires"] + row["grace"]:
            return False
        if self.grants.get(row["id"], 0) > monotonic:
            return True
        if row["remaining"] <= 0:
            return False
        duration = min(SECOND, row["remaining"], row["expires"] + row["grace"] - utc)
        self.db.execute(
            "UPDATE calls SET remaining=remaining-? WHERE session=? AND id=?",
            (duration, self.session, row["id"]),
        )
        self.grants[row["id"]] = monotonic + duration
        return True

    def _expire(self, call_id: str) -> None:
        self.db.execute(
            "UPDATE calls SET expired=1, result=NULL, result_charge=0 WHERE session=? AND id=?",
            (self.session, call_id),
        )
        self.grants.pop(call_id, None)

    def begin(
        self, call_id: str, request: bytes, *, scratch: ScratchLimits | None = None
    ) -> bytes | None:
        """Reserve before execution, or redeliver the identical unexpired result."""
        _name(call_id)
        digest = hashlib.sha256(request).hexdigest()
        expired = False
        result = None
        with self._transaction():
            session = self._owner()
            utc, monotonic = self._observe()
            row = self.db.execute(
                "SELECT * FROM calls WHERE session=? AND id=?", (self.session, call_id)
            ).fetchone()
            if row is not None:
                if row["request"] != digest:
                    raise Refused("call identity reused for a different request")
                if row["status"] != "committed":
                    raise Refused("call requires explicit recovery")
                if not self._available(row, utc, monotonic):
                    self._expire(call_id)
                    expired = True
                else:
                    result = row["result"]
                    if not isinstance(result, bytes):
                        raise Refused("saved result is not a BLOB")
                    if (
                        len(result) != row["result_charge"]
                        or len(result) > self.limits.result_bytes
                        or hashlib.sha256(result).hexdigest() != row["result_hash"]
                    ):
                        raise Refused("saved result hash or size differs")
            else:
                if session["state"] != "active":
                    raise Refused("session is retired")
                if self.db.execute(
                    "SELECT 1 FROM reservations WHERE session=? UNION ALL SELECT 1 FROM launches WHERE session=?",
                    (self.session, self.session),
                ).fetchone():
                    raise Refused("outstanding reservation requires explicit recovery")
                quota = self.db.execute("SELECT quota FROM settings WHERE id=1").fetchone()[0]
                charge = CALL_METADATA + self.limits.reservation
                if (
                    self.usage(self.session) + charge > session["quota"]
                    or self.usage() + charge > quota
                ):
                    raise Refused("quota cannot reserve maximum publication")
                if scratch is not None:
                    from .native_journal import audit_root

                    audit_root(self)
                    self.db.execute(
                        "INSERT OR IGNORE INTO scratch_settings VALUES(1,?)", (scratch.store_bytes,)
                    )
                    quota = self.db.execute("SELECT quota FROM scratch_settings").fetchone()[0]
                    used = self.db.execute(
                        "SELECT coalesce(sum(charge),0) FROM launches"
                    ).fetchone()[0]
                    if quota != scratch.store_bytes or used + scratch.bytes > quota:
                        raise Refused("scratch quota cannot admit allowance")
                self.db.execute(
                    "INSERT INTO calls(session,id,request,status,generation) VALUES(?,?,?,'pending',?)",
                    (self.session, call_id, digest, self.generation),
                )
                self.db.execute(
                    "INSERT INTO reservations VALUES(?,?,?,?)",
                    (self.session, call_id, self.generation, self.limits.reservation),
                )
                if scratch is not None:
                    self.db.execute(
                        "INSERT INTO launches VALUES(?,?,?,?,?,NULL,'prepared')",
                        (self.session, call_id, uuid.uuid4().hex, scratch.bytes, scratch.entries),
                    )
        if expired:
            raise Refused("result_expired")
        if call_id in self.grants and self.clock.monotonic_ns() >= self.grants[call_id]:
            raise Refused("grace grant elapsed; retry delivery")
        return result

    def commit(
        self,
        call_id: str,
        checkpoint: Path,
        result: bytes,
        boundary: Callable[[str], None] = lambda _: None,
    ) -> None:
        """Publish bounded checkpoint content and delivery in the reservation's transaction."""
        if len(result) > self.limits.result_bytes:
            raise Refused("result exceeds limit")
        with self._transaction():
            if self._owner()["state"] != "active":
                raise Refused("session is retired")
            row = self.db.execute(
                "SELECT status,generation FROM calls WHERE session=? AND id=?",
                (self.session, call_id),
            ).fetchone()
            reservation = self.db.execute(
                "SELECT * FROM reservations WHERE session=? AND call=?", (self.session, call_id)
            ).fetchone()
            if (
                row is None
                or tuple(row) != ("pending", self.generation)
                or reservation is None
                or reservation["generation"] != self.generation
            ):
                raise Refused("call is not reserved for this owner")
            launch = self.db.execute(
                "SELECT state,identity FROM launches WHERE session=? AND call=?",
                (self.session, call_id),
            ).fetchone()
            if launch is not None:
                from .process_identity import Identity, stopped

                if launch["state"] != "armed" or not stopped(Identity.decode(launch["identity"])):
                    raise Refused("native termination is not established")
            charge = self._capture(call_id, checkpoint)
            inventory = self._inventory(
                self.session, call_id, self.limits.checkpoint_bytes, self.limits.files
            )
            manifest_hash = self._inventory_hash(inventory)
            if charge + len(result) > reservation["charge"]:
                raise Refused("publication exceeds reservation")
            boundary("checkpoint_stored")
            boundary("before_commit")
            utc, _ = self._observe()
            grace = self.limits.grace_seconds * SECOND
            self.db.execute(
                "UPDATE calls SET status='committed',result=?,result_hash=?,result_charge=?,checkpoint_charge=?,checkpoint_hash=?,expires=?,grace=?,remaining=?,uncertain=? WHERE session=? AND id=?",
                (
                    result,
                    hashlib.sha256(result).hexdigest(),
                    len(result),
                    charge,
                    manifest_hash,
                    utc + self.limits.retention_seconds * SECOND,
                    grace,
                    grace,
                    int(self.uncertain),
                    self.session,
                    call_id,
                ),
            )
            self.db.execute(
                "UPDATE sessions SET current_call=? WHERE id=?", (call_id, self.session)
            )
            self.db.execute(
                "DELETE FROM reservations WHERE session=? AND call=?", (self.session, call_id)
            )
        boundary("after_commit")

    def _capture(self, call_id: str, root: Path) -> int:
        if root.is_symlink() or root.is_junction() or not root.is_dir():
            raise Refused("checkpoint root must be private")
        count = total = metadata = 0
        for path in root.rglob("*"):
            info = path.lstat()
            if path.is_symlink() or path.is_junction():
                raise Refused("snapshot contains a link")
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Refused("snapshot contains a non-private regular file")
            name = _relative(path.relative_to(root).as_posix()).as_posix()
            count += 1
            total += info.st_size
            if (
                count > self.limits.files
                or total > self.limits.checkpoint_bytes
                or len(name.encode()) > PATH_BYTES
            ):
                raise Refused("snapshot exceeds limit")
            digest = hashlib.sha256()
            hashes = []
            size = 0
            with path.open("rb") as stream:
                while data := stream.read(CHUNK):
                    size += len(data)
                    if size > info.st_size:
                        raise Refused("snapshot changed during capture")
                    digest.update(data)
                    chunk_hash = hashlib.sha256(data).hexdigest()
                    self.db.execute(
                        "INSERT INTO chunks VALUES(?,?) ON CONFLICT(hash) DO UPDATE SET data=excluded.data WHERE chunks.data != excluded.data",
                        (chunk_hash, zlib.compress(data, level=1)),
                    )
                    hashes.append(chunk_hash)
            if size != info.st_size:
                raise Refused("snapshot changed during capture")
            self.db.execute(
                "INSERT INTO files VALUES(?,?,?,?,?)",
                (self.session, call_id, name, size, digest.hexdigest()),
            )
            self.db.executemany(
                "INSERT INTO file_chunks VALUES(?,?,?,?,?)",
                [(self.session, call_id, name, i, h) for i, h in enumerate(hashes)],
            )
            metadata += FILE_METADATA + len(hashes) * REFERENCE_METADATA
        if count == 0:
            raise Refused("empty checkpoint")
        return total + metadata

    def _inventory(
        self, session: str, call_id: str, byte_limit: int, file_limit: int
    ) -> list[tuple[sqlite3.Row, list[str]]]:
        files = self.db.execute(
            "SELECT * FROM files WHERE session=? AND call=? ORDER BY path LIMIT ?",
            (session, call_id, file_limit + 1),
        ).fetchall()
        if not files or len(files) > file_limit:
            raise Refused("invalid checkpoint inventory")
        inventory = []
        total = 0
        for file in files:
            _relative(file["path"])
            if (
                len(file["path"].encode()) > PATH_BYTES
                or type(file["size"]) is not int
                or file["size"] < 0
                or not self._digest(file["hash"])
            ):
                raise Refused("invalid checkpoint inventory")
            total += file["size"]
            if total > byte_limit:
                raise Refused("invalid checkpoint size")
            expected = (file["size"] + CHUNK - 1) // CHUNK
            chunks = self.db.execute(
                "SELECT ordinal,hash FROM file_chunks WHERE session=? AND call=? AND path=? ORDER BY ordinal LIMIT ?",
                (session, call_id, file["path"], expected + 1),
            ).fetchall()
            if len(chunks) != expected:
                raise Refused("invalid chunk inventory")
            hashes = []
            for i, chunk in enumerate(chunks):
                if chunk["ordinal"] != i or not self._digest(chunk["hash"]):
                    raise Refused("invalid chunk sequence or hash")
                hashes.append(chunk["hash"])
            inventory.append((file, hashes))
        return inventory

    @staticmethod
    def _digest(value: object) -> bool:
        return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None

    @staticmethod
    def _inventory_hash(inventory: list[tuple[sqlite3.Row, list[str]]]) -> str:
        encoded = json.dumps(
            [[file["path"], file["size"], file["hash"], hashes] for file, hashes in inventory],
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _checked_inventory(
        self, session: str, call_id: str, byte_limit: int, file_limit: int
    ) -> list[tuple[sqlite3.Row, list[str]]]:
        row = self.db.execute(
            "SELECT status,checkpoint_hash,checkpoint_charge FROM calls WHERE session=? AND id=?",
            (session, call_id),
        ).fetchone()
        if row is None or row["status"] != "committed" or not self._digest(row["checkpoint_hash"]):
            raise Refused("checkpoint lacks a committed manifest")
        inventory = self._inventory(session, call_id, byte_limit, file_limit)
        charge = sum(
            file["size"] + FILE_METADATA + len(hashes) * REFERENCE_METADATA
            for file, hashes in inventory
        )
        if (
            charge != row["checkpoint_charge"]
            or self._inventory_hash(inventory) != row["checkpoint_hash"]
        ):
            raise Refused("checkpoint manifest or accounting differs")
        return inventory

    def _file_bytes(self, file: sqlite3.Row, hashes: list[str]) -> Iterator[bytes]:
        digest = hashlib.sha256()
        size = 0
        for chunk_hash in hashes:
            row = self.db.execute("SELECT data FROM chunks WHERE hash=?", (chunk_hash,)).fetchone()
            if row is None or not isinstance(row[0], bytes):
                raise Refused("snapshot chunk missing or invalid")
            decoder = zlib.decompressobj()
            try:
                data = decoder.decompress(row[0], CHUNK + 1)
            except zlib.error as error:
                raise Refused("invalid compressed chunk") from error
            if (
                len(data) > CHUNK
                or not decoder.eof
                or decoder.unused_data
                or hashlib.sha256(data).hexdigest() != chunk_hash
            ):
                raise Refused("snapshot chunk hash or size differs")
            size += len(data)
            if size > file["size"]:
                raise Refused("snapshot file size differs")
            digest.update(data)
            yield data
        if size != file["size"] or digest.hexdigest() != file["hash"]:
            raise Refused("snapshot file hash or size differs")

    def restore(self, destination: Path) -> str | None:
        """Restore current checkpoint independently of its result's expiry."""
        session = self._owner()
        if session["state"] != "active":
            raise Refused("session is retired")
        call_id = session["current_call"]
        if call_id is None:
            if self.db.execute(
                "SELECT 1 FROM calls WHERE session=? AND status='committed' LIMIT 1",
                (self.session,),
            ).fetchone():
                raise Refused("current checkpoint reference is missing")
            return None
        inventory = self._checked_inventory(
            self.session, call_id, self.limits.checkpoint_bytes, self.limits.files
        )
        destination.mkdir(parents=True, exist_ok=False)
        for file, hashes in inventory:
            path = destination.joinpath(*_relative(file["path"]).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                for data in self._file_bytes(file, hashes):
                    stream.write(data)
        return call_id

    def retire(self, boundary: Callable[[str], None] = lambda _: None) -> None:
        """Durably refuse new execution; outstanding reservations still require cleanup proof."""
        with self._transaction():
            self._owner()
            self.db.execute("UPDATE sessions SET state='retired' WHERE id=?", (self.session,))
            boundary("before_retire_commit")
        boundary("after_retire_commit")

    def _audit_checkpoints(self) -> None:
        if self.db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise Refused("checkpoint references are inconsistent")
        if self.db.execute(
            "SELECT 1 FROM files f JOIN calls c ON c.session=f.session AND c.id=f.call WHERE c.status!='committed' OR c.checkpoint_charge=0 LIMIT 1"
        ).fetchone():
            raise Refused("uncharged checkpoint inventory")
        if self.db.execute(
            "SELECT 1 FROM chunks c WHERE NOT EXISTS(SELECT 1 FROM file_chunks f WHERE f.hash=c.hash) LIMIT 1"
        ).fetchone():
            raise Refused("unowned checkpoint chunk")
        for session in self.db.execute("SELECT id,profile,state,current_call FROM sessions"):
            try:
                profile = json.loads(session["profile"])
                byte_limit, file_limit = profile["checkpoint_bytes"], profile["files"]
            except (ValueError, KeyError, TypeError) as error:
                raise Refused("invalid checkpoint profile") from error
            if (
                type(byte_limit) is not int
                or not 0 < byte_limit <= MAX_CHECKPOINT
                or type(file_limit) is not int
                or not 0 < file_limit <= MAX_FILES
            ):
                raise Refused("invalid checkpoint limits")
            current = session["current_call"]
            if current is not None:
                row = self.db.execute(
                    "SELECT status,checkpoint_charge FROM calls WHERE session=? AND id=?",
                    (session["id"], current),
                ).fetchone()
                if row is None or row["status"] != "committed" or row["checkpoint_charge"] <= 0:
                    raise Refused("current checkpoint reference is invalid")
            elif (
                session["state"] == "active"
                and self.db.execute(
                    "SELECT 1 FROM calls WHERE session=? AND status='committed' LIMIT 1",
                    (session["id"],),
                ).fetchone()
            ):
                raise Refused("current checkpoint reference is missing")
            for call in self.db.execute(
                "SELECT id FROM calls WHERE session=? AND checkpoint_charge>0", (session["id"],)
            ):
                inventory = self._checked_inventory(
                    session["id"], call["id"], byte_limit, file_limit
                )
                for file, hashes in inventory:
                    for _ in self._file_bytes(file, hashes):
                        pass

    def collect_checkpoints(
        self, limit: int = 1, boundary: Callable[[str], None] = lambda _: None
    ) -> int:
        """Collect a batch of obsolete checkpoints after a full store integrity pass."""
        if type(limit) is not int or not 1 <= limit <= 128:
            raise Refused("invalid checkpoint batch limit")
        count = 0
        with self._transaction():
            session = self._owner()
            if self.db.execute(
                "SELECT 1 FROM reservations WHERE session=? UNION ALL SELECT 1 FROM launches WHERE session=?",
                (self.session, self.session),
            ).fetchone():
                raise Refused("outstanding reservation requires explicit recovery")
            self._audit_checkpoints()
            calls = self.db.execute(
                "SELECT id FROM calls WHERE session=? AND checkpoint_charge>0 AND (?='retired' OR id!=?) ORDER BY id LIMIT ?",
                (self.session, session["state"], session["current_call"], limit),
            ).fetchall()
            boundary("before_collection")
            for call in calls:
                call_id = call["id"]
                hashes = self.db.execute(
                    "SELECT DISTINCT hash FROM file_chunks WHERE session=? AND call=?",
                    (self.session, call_id),
                ).fetchall()
                self.db.execute(
                    "DELETE FROM file_chunks WHERE session=? AND call=?", (self.session, call_id)
                )
                self.db.execute(
                    "DELETE FROM files WHERE session=? AND call=?", (self.session, call_id)
                )
                self.db.execute(
                    "UPDATE calls SET checkpoint_charge=0 WHERE session=? AND id=?",
                    (self.session, call_id),
                )
                self.db.execute(
                    "UPDATE sessions SET current_call=NULL WHERE id=? AND current_call=?",
                    (self.session, call_id),
                )
                self.db.executemany(
                    "DELETE FROM chunks WHERE hash=? AND NOT EXISTS(SELECT 1 FROM file_chunks WHERE hash=?)",
                    [(row[0], row[0]) for row in hashes],
                )
                count += 1
            boundary("before_collection_commit")
        boundary("after_collection_commit")
        return count

    def expire(self, limit: int = 128) -> int:
        """Examine a bounded batch of due deliveries; checkpoint and call identities remain."""
        if type(limit) is not int or not 1 <= limit <= 1024:
            raise Refused("invalid expiry batch limit")
        expired = 0
        with self._transaction():
            self._owner()
            utc, monotonic = self._observe()
            for row in self.db.execute(
                "SELECT id,expires,grace,remaining,uncertain,expired FROM calls WHERE session=? AND status='committed' AND expired=0 AND expires<=? ORDER BY expires,id LIMIT ?",
                (self.session, utc, limit),
            ).fetchall():
                if not self._available(row, utc, monotonic):
                    self._expire(row["id"])
                    expired += 1
        return expired
