"""Conservative process-crash recovery for a private, same-machine scratch tree."""

from __future__ import annotations

import re
import sqlite3
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from .host_store import Refused
from .process_identity import Identity, capture, stopped

if TYPE_CHECKING:
    from .shared_store import SharedStore


def audit_root(store: SharedStore) -> Path:
    """Refuse unowned scratch; callers hold the database transaction and session lock."""
    root = store.root / "scratch"
    if root.is_symlink() or root.is_junction():
        raise Refused("scratch root is not private")
    records = store.db.execute("SELECT token FROM launches").fetchall()
    tokens = {row[0] for row in records}
    if any(
        not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token) for token in tokens
    ):
        raise Refused("invalid scratch token")
    if root.exists():
        for path in root.iterdir():
            if (
                path.name not in tokens
                or path.is_symlink()
                or path.is_junction()
                or not path.is_dir()
            ):
                raise Refused("unknown or nonprivate scratch blocks admission and cleanup")
    return root


class NativeJournal:
    """Keep temporary capacity charged until the recorded helper and its files are gone."""

    def __init__(self, store: SharedStore):
        self.store = store

    def _row(self, call: str) -> sqlite3.Row:
        self.store._owner()
        row = self.store.db.execute(
            "SELECT l.*,c.status,c.generation FROM launches l JOIN calls c ON c.session=l.session AND c.id=l.call WHERE l.session=? AND l.call=?",
            (self.store.session, call),
        ).fetchone()
        if row is None:
            raise Refused("call has no scratch reservation")
        return row

    def prepare(self, call: str) -> Path:
        """Create scratch only after admission; an existing directory is never reused."""
        with self.store._transaction():
            row = self._row(call)
            if (
                self.store._owner()["state"] != "active"
                or row["status"] != "pending"
                or row["generation"] != self.store.generation
                or row["state"] != "prepared"
            ):
                raise Refused("call is not prepared for this owner")
            root = audit_root(self.store)
            root.mkdir(exist_ok=True)
            path = root / row["token"]
            path.mkdir()
            return path

    def arm(
        self,
        call: str,
        child: subprocess.Popen[bytes],
        boundary: Callable[[str], None] = lambda _: None,
    ) -> None:
        """Persist the blocked helper's identity before its supervisor sends the header."""
        identity = capture(child.pid)
        with self.store._transaction():
            row = self._row(call)
            if (
                self.store._owner()["state"] != "active"
                or row["status"] != "pending"
                or row["generation"] != self.store.generation
                or row["state"] != "prepared"
                or row["identity"] is not None
            ):
                raise Refused("launch is not prepared for this owner")
            path = audit_root(self.store) / row["token"]
            if not path.is_dir() or child.poll() is not None:
                raise Refused("helper or reserved scratch is unavailable")
            self.store.db.execute(
                "UPDATE launches SET identity=?,state='armed' WHERE session=? AND call=?",
                (identity.encode(), self.store.session, call),
            )
            boundary("before_launch_commit")
        boundary("after_launch_commit")

    def reclaim(self, call: str, boundary: Callable[[str], None] = lambda _: None) -> None:
        """Record cleanup intent, remove verified private entries, then release allowances."""
        with self.store._transaction():
            row = self._row(call)
            root = audit_root(self.store)
            if row["identity"] is None or row["state"] not in ("armed", "cleaning"):
                raise Refused("unidentified launch requires explicit reconciliation")
            if not stopped(Identity.decode(row["identity"])):
                raise Refused("helper is still alive")
            path = root / row["token"]
            self._inventory(path, row["charge"], row["entries"])
            self.store.db.execute(
                "UPDATE launches SET state='cleaning' WHERE session=? AND call=?",
                (self.store.session, call),
            )
            boundary("before_cleanup_intent")
        boundary("after_cleanup_intent")
        # The session lock excludes another supervisor; this experiment assumes no external writer.
        files, directories = self._inventory(path, row["charge"], row["entries"])
        for file in files:
            file.unlink()
        for directory in reversed(directories):
            directory.rmdir()
        boundary("after_scratch_removal")
        with self.store._transaction():
            current = self._row(call)
            root = audit_root(self.store)
            if current["state"] != "cleaning" or current["identity"] != row["identity"]:
                raise Refused("cleanup identity changed")
            if (root / row["token"]).exists():
                raise Refused("scratch reclamation is incomplete")
            self.store.db.execute(
                "UPDATE calls SET status='interrupted' WHERE session=? AND id=? AND status='pending'",
                (self.store.session, call),
            )
            self.store.db.execute(
                "DELETE FROM reservations WHERE session=? AND call=?", (self.store.session, call)
            )
            self.store.db.execute(
                "DELETE FROM launches WHERE session=? AND call=?", (self.store.session, call)
            )
            boundary("before_cleanup_release")
        boundary("after_cleanup_release")

    def _inventory(
        self, path: Path, byte_limit: int, entry_limit: int
    ) -> tuple[list[Path], list[Path]]:
        root = self.store.root / "scratch"
        if path.parent != root or path.resolve().parent != root.resolve():
            raise Refused("scratch target escaped its managed root")
        if path.is_symlink() or path.is_junction():
            raise Refused("scratch target is not private")
        if not path.exists():
            return [], []
        files: list[Path] = []
        directories = [path]
        pending = [path]
        total = 0
        while pending:
            directory = pending.pop()
            for entry in directory.iterdir():
                info = entry.lstat()
                if entry.is_symlink() or entry.is_junction():
                    raise Refused("scratch contains links")
                if stat.S_ISDIR(info.st_mode):
                    directories.append(entry)
                    pending.append(entry)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    files.append(entry)
                    total += info.st_size
                else:
                    raise Refused("scratch contains nonprivate entries")
                if total > byte_limit or len(files) + len(directories) > entry_limit:
                    raise Refused("scratch exceeds its reserved bounds")
        return files, directories
