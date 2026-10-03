"""Experimental local checkpoint/result transactions; one trusted owner per database."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import zlib
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import BinaryIO

CHUNK = 1024 * 1024
MAX_CHECKPOINT = 2 * 1024**3
MAX_FILES = 128
MAX_RESULT = 2 * CHUNK


class Refused(RuntimeError):
    """An operation cannot preserve session identity or committed state."""


def _name(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value):
        raise Refused("invalid call identity")
    return value


def _relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or path.as_posix() != value:
        raise Refused("invalid snapshot path")
    for part in path.parts:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*", part):
            raise Refused("invalid snapshot component")
        if part.split(".")[0].upper() in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(10)),
            *(f"LPT{i}" for i in range(10)),
        }:
            raise Refused("reserved snapshot component")
    return path


def _lock(stream: BinaryIO, acquire: bool) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN)


class Store:
    """Own one private local store; network filesystems and remote takeover are unsupported."""

    def __init__(self, root: Path, profile: dict[str, str]):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = (self.root / "owner.lock").open("a+b")
        if self.lock.seek(0, os.SEEK_END) == 0:
            self.lock.write(b"0")
            self.lock.flush()
        try:
            _lock(self.lock, True)
        except OSError as error:
            self.lock.close()
            raise Refused("session already has an owner") from error
        try:
            existed = (self.root / "state.sqlite").exists()
            self.db = sqlite3.connect(self.root / "state.sqlite", timeout=0)
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (
                    id INTEGER PRIMARY KEY CHECK(id=1), profile TEXT NOT NULL,
                    generation INTEGER NOT NULL, current_call TEXT);
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL,
                    generation INTEGER NOT NULL, result BLOB, result_hash TEXT);
                CREATE TABLE IF NOT EXISTS chunks (hash TEXT PRIMARY KEY, data BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS files (
                    call_id TEXT NOT NULL REFERENCES calls(id), path TEXT NOT NULL,
                    size INTEGER NOT NULL, hash TEXT NOT NULL, chunks TEXT NOT NULL,
                    PRIMARY KEY(call_id, path));
            """)
            encoded = json.dumps(profile, sort_keys=True, separators=(",", ":"))
            with self.db:
                row = self.db.execute("SELECT profile FROM meta WHERE id=1").fetchone()
                if row is None:
                    if existed:
                        raise Refused("existing store has no session metadata")
                    self.db.execute("INSERT INTO meta VALUES(1, ?, 0, NULL)", (encoded,))
                elif row[0] != encoded:
                    raise Refused("runtime, policy, machine or session profile differs")
                self.db.execute("UPDATE meta SET generation=generation+1 WHERE id=1")
                self.db.execute("UPDATE calls SET status='interrupted' WHERE status='pending'")
            self.generation = self.db.execute("SELECT generation FROM meta WHERE id=1").fetchone()[
                0
            ]
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self.lock.close()
            raise

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Release local ownership; a later owner receives a new generation."""
        self.db.close()
        self.lock.close()

    def _owner(self) -> None:
        if (
            self.lock.closed
            or self.db.execute("SELECT generation FROM meta WHERE id=1").fetchone()[0]
            != self.generation
        ):
            raise Refused("owner generation changed")

    def begin(self, call_id: str, request: bytes) -> bytes | None:
        """Persist intent, or redeliver a matching committed result without execution."""
        self._owner()
        _name(call_id)
        digest = hashlib.sha256(request).hexdigest()
        row = self.db.execute(
            "SELECT request, status, result, result_hash FROM calls WHERE id=?", (call_id,)
        ).fetchone()
        if row is not None:
            if row[0] != digest:
                raise Refused("call identity reused for a different request")
            if row[1] != "committed":
                raise Refused("call outcome is not committed; explicit recovery is required")
            result = bytes(row[2])
            if len(result) > MAX_RESULT or hashlib.sha256(result).hexdigest() != row[3]:
                raise Refused("saved result hash or size differs")
            return result
        if self.db.execute("SELECT 1 FROM calls WHERE status='pending'").fetchone():
            raise Refused("an uncommitted call requires explicit recovery")
        with self.db:
            self.db.execute(
                "INSERT INTO calls VALUES(?, ?, 'pending', ?, NULL, NULL)",
                (call_id, digest, self.generation),
            )
        return None

    def commit(
        self,
        call_id: str,
        checkpoint: Path,
        result: bytes,
        boundary: Callable[[str], None] = lambda _: None,
    ) -> None:
        """Publish snapshot bytes and result in one FULL-synchronous SQLite transaction."""
        self._owner()
        if len(result) > MAX_RESULT:
            raise Refused("result exceeds limit")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT status, generation FROM calls WHERE id=?", (call_id,)
            ).fetchone()
            if row != ("pending", self.generation):
                raise Refused("call is not pending for this owner")
            if checkpoint.is_symlink() or checkpoint.is_junction():
                raise Refused("checkpoint root must be a private directory")
            count = total = 0
            for path in sorted(checkpoint.rglob("*")):
                info = path.lstat()
                if path.is_symlink() or path.is_junction():
                    raise Refused("snapshot contains a link")
                if stat.S_ISDIR(info.st_mode):
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise Refused("snapshot contains a non-private regular file")
                name = _relative(path.relative_to(checkpoint).as_posix()).as_posix()
                count += 1
                total += info.st_size
                if count > MAX_FILES or total > MAX_CHECKPOINT:
                    raise Refused("snapshot exceeds limit")
                digest = hashlib.sha256()
                hashes: list[str] = []
                size = 0
                with path.open("rb") as stream:
                    while data := stream.read(CHUNK):
                        size += len(data)
                        if size > info.st_size:
                            raise Refused("snapshot changed during capture")
                        digest.update(data)
                        chunk_hash = hashlib.sha256(data).hexdigest()
                        self.db.execute(
                            "INSERT INTO chunks VALUES(?, ?) "
                            "ON CONFLICT(hash) DO UPDATE SET data=excluded.data "
                            "WHERE chunks.data != excluded.data",
                            (chunk_hash, zlib.compress(data, level=1)),
                        )
                        hashes.append(chunk_hash)
                if size != info.st_size:
                    raise Refused("snapshot changed during capture")
                self.db.execute(
                    "INSERT INTO files VALUES(?, ?, ?, ?, ?)",
                    (call_id, name, size, digest.hexdigest(), json.dumps(hashes)),
                )
            if count == 0:
                raise Refused("empty checkpoint")
            boundary("checkpoint_stored")
            self.db.execute(
                "UPDATE calls SET status='committed', result=?, result_hash=? WHERE id=?",
                (result, hashlib.sha256(result).hexdigest(), call_id),
            )
            self.db.execute("UPDATE meta SET current_call=? WHERE id=1", (call_id,))
            boundary("before_commit")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        boundary("after_commit")

    def restore(self, destination: Path) -> str | None:
        """Materialize and verify the latest committed snapshot in a new private directory."""
        self._owner()
        call_id = self.db.execute("SELECT current_call FROM meta WHERE id=1").fetchone()[0]
        if call_id is None:
            return None
        row = self.db.execute("SELECT status FROM calls WHERE id=?", (call_id,)).fetchone()
        if row != ("committed",):
            raise Refused("checkpoint lacks a committed call")
        destination.mkdir(parents=True, exist_ok=False)
        rows = self.db.execute(
            "SELECT path, size, hash, chunks FROM files WHERE call_id=? ORDER BY path", (call_id,)
        ).fetchall()
        if not rows or len(rows) > MAX_FILES:
            raise Refused("invalid checkpoint inventory")
        total = 0
        for name, expected_size, expected_hash, encoded in rows:
            path = destination.joinpath(*_relative(name).parts)
            total += expected_size
            if expected_size < 0 or total > MAX_CHECKPOINT:
                raise Refused("invalid snapshot size")
            hashes = json.loads(encoded)
            if not isinstance(hashes, list) or len(hashes) != (expected_size + CHUNK - 1) // CHUNK:
                raise Refused("invalid chunk inventory")
            path.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with path.open("xb") as stream:
                for chunk_hash in hashes:
                    row = self.db.execute(
                        "SELECT data FROM chunks WHERE hash=?", (chunk_hash,)
                    ).fetchone()
                    if row is None:
                        raise Refused("snapshot chunk missing")
                    decoder = zlib.decompressobj()
                    data = decoder.decompress(row[0], CHUNK + 1)
                    if len(data) > CHUNK or not decoder.eof or decoder.unused_data:
                        raise Refused("invalid compressed chunk")
                    if hashlib.sha256(data).hexdigest() != chunk_hash:
                        raise Refused("snapshot chunk hash differs")
                    size += len(data)
                    digest.update(data)
                    stream.write(data)
            if size != expected_size or digest.hexdigest() != expected_hash:
                raise Refused("snapshot file hash or size differs")
        return call_id
