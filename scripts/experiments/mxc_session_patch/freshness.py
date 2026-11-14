"""Opt-in rollback detection using a host-retained SQLite freshness record."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from .host_store import Refused, _check_rollback_format, _lock
from .private_root import check_file, prepare

MARKER = (
    "CREATE TABLE freshness_marker(store TEXT NOT NULL, role TEXT NOT NULL, revision TEXT NOT NULL)"
)
_HEADS = "CREATE TABLE heads(role TEXT PRIMARY KEY, revision TEXT)"
_IDENTITY = "CREATE TABLE identity(store TEXT NOT NULL)"
_ROLES = {"ownership", "publication"}


class Witness:
    """Own a separate freshness root that the host must exclude from journal restores."""

    def __init__(self, root: Path, coordinator: Path, *, initialize: bool = False):
        self.root = root.resolve()
        service = coordinator.resolve()
        if self.root.is_relative_to(service) or service.is_relative_to(self.root):
            raise Refused("freshness storage must be outside the coordinator root")
        if not initialize and not root.is_dir():
            raise Refused("freshness storage is unavailable")
        prepare(root)
        self.path = self.root / "freshness.sqlite"
        self.initializing = initialize
        check_file(self.root / "service.lock")
        self.lock = (self.root / "service.lock").open("a+b")
        try:
            if self.lock.seek(0, 2) == 0:
                self.lock.write(b"0")
                self.lock.flush()
            _lock(self.lock, True)
            if initialize and any(p.name != "service.lock" for p in self.root.iterdir()):
                raise Refused("freshness initialization requires an empty root")
            self._files()
            with closing(self._connect(create=initialize)) as db:
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                if initialize:
                    db.execute(_IDENTITY)
                    db.execute(_HEADS)
                    db.execute("INSERT INTO identity VALUES(?)", (uuid.uuid4().hex,))
                    db.executemany("INSERT INTO heads VALUES(?,NULL)", [(r,) for r in _ROLES])
                self.store = self._read(db, complete=not initialize)[0]
                db.commit()
        except BaseException as error:
            self.close()
            if isinstance(error, (OSError, sqlite3.Error)):
                raise Refused("freshness storage unavailable or corrupt") from error
            raise

    def _files(self) -> None:
        for suffix in ("", "-journal", "-wal", "-shm"):
            check_file(Path(str(self.path) + suffix))
        _check_rollback_format(self.path)

    def _connect(self, *, create: bool = False) -> sqlite3.Connection:
        return sqlite3.connect(
            self.path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
            uri=True,
            timeout=0,
            isolation_level=None,
        )

    @staticmethod
    def _read(db: sqlite3.Connection, *, complete: bool) -> tuple[str, dict[str, str | None]]:
        schema = {
            r[0] for r in db.execute("SELECT sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*'")
        }
        if schema != {_HEADS, _IDENTITY}:
            raise Refused("unsupported freshness schema")
        identity = db.execute("SELECT store FROM identity").fetchall()
        heads = dict(db.execute("SELECT role,revision FROM heads"))
        if (
            len(identity) != 1
            or not isinstance(identity[0][0], str)
            or len(identity[0][0]) != 32
            or set(heads) != _ROLES
            or any(
                (r is None and complete)
                or (r is not None and (not isinstance(r, str) or len(r) != 32))
                for r in heads.values()
            )
            or db.execute("PRAGMA quick_check").fetchone()[0] != "ok"
        ):
            raise Refused("invalid or incomplete freshness record")
        return identity[0][0], heads

    def head(self, role: str) -> str | None:
        if self.lock.closed:
            raise Refused("freshness record is closed")
        self._files()
        with closing(self._connect()) as db:
            store, heads = self._read(db, complete=not self.initializing)
            if store != self.store:
                raise Refused("freshness store identity differs")
            return heads[role]

    def ready(self) -> None:
        """Require both journals before the coordinator can serve requests."""
        self.initializing = False
        self.head("ownership")

    def close(self) -> None:
        self.lock.close()

    def validate(self, db: sqlite3.Connection, role: str) -> str | None:
        expected = self.head(role)
        schema = db.execute(
            "SELECT sql FROM sqlite_schema WHERE name='freshness_marker'"
        ).fetchone()
        if schema is None:
            if (
                self.initializing
                and expected is None
                and db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone() is None
            ):
                return None
            raise Refused("freshness protection requires a fresh journal; migration is unavailable")
        if schema[0] != MARKER or [
            tuple(r) for r in db.execute("SELECT store,role,revision FROM freshness_marker")
        ] != [(self.store, role, expected)]:
            raise Refused("journal differs from the retained freshness record")
        return expected

    def advance(self, db: sqlite3.Connection, role: str, previous: str | None) -> None:
        # Attach only at commit: ownership guards can surround publication transactions.
        self._files()
        db.execute("ATTACH DATABASE ? AS freshness", (self.path.as_uri() + "?mode=rw",))
        if (
            db.execute("PRAGMA main.journal_mode").fetchone()[0] != "delete"
            or db.execute("PRAGMA freshness.journal_mode").fetchone()[0] != "delete"
            or db.execute("PRAGMA main.synchronous").fetchone()[0] < 2
            or db.execute("PRAGMA freshness.synchronous").fetchone()[0] < 2
        ):
            raise Refused("freshness commits require durable rollback journals")
        if [tuple(r) for r in db.execute("SELECT store FROM freshness.identity")] != [
            (self.store,)
        ]:
            raise Refused("freshness store identity differs")
        revision = uuid.uuid4().hex
        changed = db.execute(
            "UPDATE freshness.heads SET revision=? WHERE role=? AND revision IS ?",
            (revision, role, previous),
        ).rowcount
        if changed != 1:
            raise Refused("freshness revision changed during the transaction")
        if previous is None:
            db.execute(MARKER)
            db.execute("INSERT INTO freshness_marker VALUES(?,?,?)", (self.store, role, revision))
        else:
            db.execute("UPDATE freshness_marker SET revision=?", (revision,))


@contextmanager
def transaction(db: sqlite3.Connection, witness: Witness | None, role: str) -> Iterator[None]:
    """Commit journal changes and their retained revision in one SQLite transaction."""
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            previous = None
            if witness is None:
                if db.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name='freshness_marker'"
                ).fetchone():
                    raise Refused("protected journal requires its freshness record")
            else:
                previous = witness.validate(db, role)
            changes = db.total_changes
            schema = db.execute("PRAGMA schema_version").fetchone()[0]
            yield
            if witness is not None:
                if (
                    previous is None
                    or changes != db.total_changes
                    or schema != db.execute("PRAGMA schema_version").fetchone()[0]
                ):
                    witness.advance(db, role, previous)
                else:
                    witness.validate(db, role)
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            if any(row[1] == "freshness" for row in db.execute("PRAGMA database_list")):
                db.execute("DETACH DATABASE freshness")
    except (OSError, sqlite3.Error) as error:
        if witness is None:
            raise
        raise Refused("journal or freshness storage unavailable or corrupt") from error
