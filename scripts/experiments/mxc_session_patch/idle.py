"""Persist opt-in idle retirement independently of completed-result retention."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .accounting import MAX_INTEGER
from .host_store import Refused

if TYPE_CHECKING:
    from .shared_store import SharedStore

VERSION = 5
SECOND = 1_000_000_000
SCHEMA = "CREATE TABLE session_idle(session TEXT PRIMARY KEY REFERENCES sessions(id), timeout INTEGER NOT NULL CHECK(timeout>0), grace INTEGER NOT NULL CHECK(grace>=0), deadline INTEGER CHECK(deadline>=0), remaining INTEGER NOT NULL CHECK(remaining>=0 AND remaining<=grace), uncertain INTEGER NOT NULL CHECK(uncertain IN (0,1)))"


@dataclass(frozen=True)
class Policy:
    """Host-selected idle timeout and finite forgiveness for uncertain host clocks."""

    timeout_seconds: int
    grace_seconds: int = 300

    def __post_init__(self) -> None:
        if (
            type(self.timeout_seconds) is not int
            or type(self.grace_seconds) is not int
            or self.timeout_seconds <= 0
            or self.grace_seconds < 0
            or (self.timeout_seconds + self.grace_seconds) * SECOND > MAX_INTEGER
        ):
            raise Refused("invalid idle policy")


def validate_schema(db: sqlite3.Connection) -> None:
    """Refuse a missing or altered lifecycle schema before opening the owner."""
    row = db.execute("SELECT sql FROM sqlite_master WHERE name='session_idle'").fetchone()
    if row is None or row[0] != SCHEMA:
        raise Refused("unsupported or corrupt store format")


def _busy(store: SharedStore) -> bool:
    return (
        store.db.execute(
            "SELECT 1 FROM reservations WHERE session=? UNION ALL SELECT 1 FROM launches WHERE session=?",
            (store.session, store.session),
        ).fetchone()
        is not None
    )


def _deadline(store: SharedStore, timeout: int, grace: int) -> int:
    utc, _ = store._observe()
    if utc + timeout + grace > MAX_INTEGER:
        raise Refused("idle deadline exceeds timestamp range")
    return utc + timeout


def configure(store: SharedStore, policy: Policy | None) -> bool:
    """Configure under the session lock; existing policy and deadlines cannot be replaced."""
    version = store.db.execute("SELECT version FROM settings WHERE id=1").fetchone()[0]
    if version != VERSION:
        if policy is None:
            return False
        store.db.execute(SCHEMA)
        store.db.execute("UPDATE settings SET version=? WHERE id=1", (VERSION,))
    row = store.db.execute(
        "SELECT * FROM session_idle WHERE session=?", (store.session,)
    ).fetchone()
    if row is None:
        if policy is None:
            return False
        generation = store.db.execute(
            "SELECT generation FROM sessions WHERE id=?", (store.session,)
        ).fetchone()[0]
        if generation != 0:
            raise Refused("missing idle record")
        timeout, grace = policy.timeout_seconds * SECOND, policy.grace_seconds * SECOND
        deadline = None if _busy(store) else _deadline(store, timeout, grace)
        store.db.execute(
            "INSERT INTO session_idle VALUES(?,?,?,?,?,0)",
            (store.session, timeout, grace, deadline, grace),
        )
    elif policy is None or (row["timeout"], row["grace"]) != (
        policy.timeout_seconds * SECOND,
        policy.grace_seconds * SECOND,
    ):
        raise Refused("session idle policy configuration differs")
    else:
        _checked(row)
        store.db.execute("UPDATE session_idle SET uncertain=1 WHERE session=?", (store.session,))
    return True


def _checked(row: sqlite3.Row) -> None:
    values = (row["timeout"], row["grace"], row["remaining"], row["uncertain"])
    if (
        any(type(value) is not int or not 0 <= value <= MAX_INTEGER for value in values)
        or row["timeout"] <= 0
        or row["timeout"] + row["grace"] > MAX_INTEGER
        or row["remaining"] > row["grace"]
        or row["uncertain"] not in (0, 1)
        or (
            row["deadline"] is not None
            and (
                type(row["deadline"]) is not int
                or not 0 <= row["deadline"] <= MAX_INTEGER - row["grace"]
            )
        )
    ):
        raise Refused("invalid idle record")


def _row(store: SharedStore) -> sqlite3.Row:
    row = store.db.execute(
        "SELECT * FROM session_idle WHERE session=?", (store.session,)
    ).fetchone()
    if row is None:
        raise Refused("missing idle record")
    _checked(row)
    return row


def start(store: SharedStore) -> None:
    """Suspend the idle interval in the admission transaction."""
    if store.idle_enabled:
        _row(store)
        store.db.execute("UPDATE session_idle SET deadline=NULL WHERE session=?", (store.session,))
        store.idle_grant = 0


def finish(store: SharedStore) -> None:
    """Start a full idle interval only once all execution and scratch charges are reconciled."""
    if not store.idle_enabled or _busy(store):
        return
    row = _row(store)
    if row["deadline"] is not None:
        return
    deadline = _deadline(store, row["timeout"], row["grace"])
    store.db.execute(
        "UPDATE session_idle SET deadline=?,remaining=grace,uncertain=0 WHERE session=?",
        (deadline, store.session),
    )
    store.idle_grant = 0


def retire(store: SharedStore, boundary: Callable[[str], None]) -> bool:
    """Spend persisted forgiveness before retiring an unoccupied session."""
    if not store.idle_enabled:
        return False
    retired = False
    with store._transaction():
        if store._owner()["state"] == "retired":
            return False
        row = _row(store)
        if _busy(store):
            return False
        if row["deadline"] is None:
            raise Refused("idle completion requires explicit recovery")
        utc, monotonic = store._observe()
        row = _row(store)
        if utc < row["deadline"]:
            store.idle_grant = 0
            return False
        if row["uncertain"] and utc < row["deadline"] + row["grace"]:
            if store.idle_grant > monotonic:
                return False
            if row["remaining"] > 0:
                duration = min(SECOND, row["remaining"], row["deadline"] + row["grace"] - utc)
                store.db.execute(
                    "UPDATE session_idle SET remaining=remaining-? WHERE session=?",
                    (duration, store.session),
                )
                store.idle_grant = monotonic + duration
                boundary("before_idle_grant_commit")
            else:
                retired = True
        else:
            retired = True
        if retired:
            store.db.execute("UPDATE sessions SET state='retired' WHERE id=?", (store.session,))
            boundary("before_retire_commit")
    boundary("after_retire_commit" if retired else "after_idle_grant_commit")
    return retired
