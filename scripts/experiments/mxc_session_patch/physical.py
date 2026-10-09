"""Opt-in SQLite allocation ceiling and conservative volume admission checks."""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .accounting import MAX_INTEGER
from .host_store import Refused

if TYPE_CHECKING:
    from .shared_store import SharedStore

VERSION = 6
SCHEMA = "CREATE TABLE physical_settings(id INTEGER PRIMARY KEY CHECK(id=1), database_bytes INTEGER NOT NULL CHECK(database_bytes>0), transaction_bytes INTEGER NOT NULL CHECK(transaction_bytes>0), minimum_free_bytes INTEGER NOT NULL CHECK(minimum_free_bytes>=0))"


@dataclass(frozen=True)
class Policy:
    """Fixed root-wide database ceiling and host-selected journal/volume headroom, in bytes."""

    database_bytes: int
    transaction_bytes: int
    minimum_free_bytes: int

    def __post_init__(self) -> None:
        for value in (self.database_bytes, self.transaction_bytes, self.minimum_free_bytes):
            if type(value) is not int or not 0 <= value <= MAX_INTEGER:
                raise Refused("invalid physical storage policy")
        if not self.database_bytes or not self.transaction_bytes:
            raise Refused("invalid physical storage policy")


def validate(db: sqlite3.Connection, policy: Policy | None) -> None:
    """Require the same explicit policy before a format-6 owner can write."""
    schema = db.execute("SELECT sql FROM sqlite_master WHERE name='physical_settings'").fetchone()
    if schema is None or schema[0] != SCHEMA:
        raise Refused("unsupported physical storage schema")
    rows = db.execute("SELECT * FROM physical_settings").fetchall()
    if (
        policy is None
        or len(rows) != 1
        or tuple(rows[0])
        != (1, policy.database_bytes, policy.transaction_bytes, policy.minimum_free_bytes)
    ):
        raise Refused("physical storage policy configuration differs")


def configure_connection(store: SharedStore) -> None:
    """Reapply and verify connection-local limits before any schema or owner writes."""
    policy = store.physical_policy
    if policy is None:
        return
    db = store.db
    page_size = db.execute("PRAGMA page_size").fetchone()[0]
    if policy.database_bytes % page_size:
        raise Refused("database ceiling must be a multiple of SQLite page size")
    pages = policy.database_bytes // page_size
    if pages < 1 or db.execute(f"PRAGMA max_page_count={pages}").fetchone()[0] != pages:
        raise Refused("database cannot satisfy physical ceiling")
    if (store.root / "shared.sqlite").stat().st_size > policy.database_bytes:
        raise Refused("database exceeds physical ceiling")
    db.execute("PRAGMA temp_store=MEMORY")
    if db.execute("PRAGMA temp_store").fetchone()[0] != 2:
        raise Refused("SQLite temporary storage must remain in memory")
    if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
        raise Refused("physical policy requires DELETE journaling")


def initialize(store: SharedStore) -> None:
    """Persist a new root's policy in its schema transaction."""
    policy = store.physical_policy
    if policy is not None:
        store.db.execute(SCHEMA)
        store.db.execute(
            "INSERT INTO physical_settings VALUES(1,?,?,?)",
            (policy.database_bytes, policy.transaction_bytes, policy.minimum_free_bytes),
        )
        store.db.execute("UPDATE settings SET version=? WHERE id=1", (VERSION,))


def check_headroom(
    store: SharedStore, *, extra_scratch: int = 0, initializing: bool = False
) -> None:
    """Check before growth; existing scratch is conservatively charged at its full allowance."""
    policy = store.physical_policy
    if policy is None:
        return
    if not store.db.in_transaction:
        raise Refused("physical admission requires a write transaction")
    db = store.db
    page_size = db.execute("PRAGMA page_size").fetchone()[0]
    allocated = db.execute("PRAGMA page_count").fetchone()[0] * page_size
    if allocated > policy.database_bytes:
        raise Refused("database exceeds physical ceiling")
    allocated = min(allocated, (store.root / "shared.sqlite").stat().st_size)
    scratch = (
        0
        if initializing
        else db.execute("SELECT coalesce(sum(charge),0) FROM launches").fetchone()[0]
    )
    required = (
        policy.database_bytes
        - allocated
        + policy.transaction_bytes
        + policy.minimum_free_bytes
        + scratch
        + extra_scratch
    )
    try:
        free = shutil.disk_usage(store.root).free
    except OSError as error:
        raise Refused("physical free-space observation unavailable") from error
    if type(free) is not int or free < required:
        raise Refused("insufficient physical storage headroom")
