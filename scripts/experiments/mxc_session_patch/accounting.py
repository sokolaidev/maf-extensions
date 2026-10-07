"""Transactional logical charges and independent retained-record audits."""

from __future__ import annotations

import sqlite3

from .host_store import Refused, _name

STORE_METADATA = 4096
SESSION_METADATA = 20480
CALL_METADATA = 2048
MAX_INTEGER = 2**63 - 1


def _adjust(session: str, delta: str) -> str:
    return " ".join(
        f"UPDATE logical_usage SET charge=charge+({delta}) WHERE session={scope}; "
        "SELECT CASE WHEN changes()!=1 THEN RAISE(ABORT,'missing logical total') END;"
        for scope in (session, "''")
    )


def _triggers() -> dict[str, str]:
    bodies = {
        "sessions_insert": (
            "AFTER INSERT ON sessions",
            (
                f"INSERT INTO logical_usage VALUES(NEW.id,{SESSION_METADATA}); "
                f"UPDATE logical_usage SET charge=charge+{SESSION_METADATA} WHERE session=''; "
                "SELECT CASE WHEN changes()!=1 THEN RAISE(ABORT,'missing logical total') END;"
            ),
        ),
        "sessions_delete": (
            "AFTER DELETE ON sessions",
            (
                f"UPDATE logical_usage SET charge=charge-{SESSION_METADATA} WHERE session=''; "
                "SELECT CASE WHEN changes()!=1 THEN RAISE(ABORT,'missing logical total') END; "
                "DELETE FROM logical_usage WHERE session=OLD.id;"
            ),
        ),
    }
    for table, key in (
        ("sessions", "id"),
        ("calls", "session,id"),
        ("reservations", "session,call"),
    ):
        changed = " OR ".join(f"OLD.{column} IS NOT NEW.{column}" for column in key.split(","))
        bodies[f"{table}_identity"] = (
            f"BEFORE UPDATE OF {key} ON {table} WHEN {changed}",
            "SELECT RAISE(ABORT,'accounted identity is immutable');",
        )
    for table, columns, metadata in (
        ("calls", ("result_charge", "checkpoint_charge"), CALL_METADATA),
        ("reservations", ("charge",), 0),
    ):
        for event, aliases in (
            ("INSERT", ("NEW",)),
            ("DELETE", ("OLD",)),
            ("UPDATE", ("OLD", "NEW")),
        ):
            invalid = " OR ".join(
                f"typeof({alias}.{column})!='integer' OR {alias}.{column}<0"
                for alias in aliases
                for column in columns
            )
            guard = f"SELECT CASE WHEN {invalid} THEN RAISE(ABORT,'invalid logical charge') END; "
            if event == "UPDATE":
                delta = "+".join(f"(NEW.{column}-OLD.{column})" for column in columns)
                timing = f"AFTER UPDATE OF {','.join(columns)} ON {table}"
            else:
                value = "+".join((str(metadata), *(f"{aliases[0]}.{c}" for c in columns)))
                delta = f"-({value})" if event == "DELETE" else value
                timing = f"AFTER {event} ON {table}"
            bodies[f"{table}_{event.lower()}"] = (
                timing,
                guard + _adjust(f"{aliases[0]}.session", delta),
            )
    return {
        f"logical_{name}": f"CREATE TRIGGER logical_{name} {timing} BEGIN {body} END"
        for name, (timing, body) in bodies.items()
    }


def _integer(value: object, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise Refused("invalid logical accounting value")
    return value


def scan(db: sqlite3.Connection) -> dict[str, int]:
    """Recompute logical charges from source rows without trusting stored totals."""
    totals = {"": STORE_METADATA}
    quotas = {}
    settings = db.execute("SELECT quota FROM settings WHERE id=1").fetchone()
    if settings is None:
        raise Refused("missing store quota")
    quotas[""] = _integer(settings[0], STORE_METADATA)
    for session, quota in db.execute("SELECT id,quota FROM sessions"):
        _name(session)
        totals[session] = SESSION_METADATA
        quotas[session] = _integer(quota, SESSION_METADATA)
    for table, columns, metadata in (
        ("calls", "result_charge,checkpoint_charge", CALL_METADATA),
        ("reservations", "charge", 0),
    ):
        for row in db.execute(f"SELECT session,{columns} FROM {table}"):
            if row[0] not in quotas or row[0] == "":
                raise Refused("unowned logical charge")
            totals[row[0]] += metadata + sum(_integer(value) for value in row[1:])
    totals[""] += sum(value for session, value in totals.items() if session)
    if any(_integer(value) > quotas[session] for session, value in totals.items()):
        raise Refused("logical accounting exceeds quota")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise Refused("logical accounting references are inconsistent")
    return totals


def initialize(db: sqlite3.Connection) -> None:
    """Install totals and their triggers inside the caller's initialization transaction."""
    totals = scan(db)
    db.execute(
        "CREATE TABLE logical_usage(session TEXT PRIMARY KEY NOT NULL, charge INTEGER NOT NULL "
        "CHECK(typeof(charge)='integer' AND charge>=0))"
    )
    db.executemany("INSERT INTO logical_usage VALUES(?,?)", totals.items())
    for statement in _triggers().values():
        db.execute(statement)


def audit(db: sqlite3.Connection) -> None:
    """Refuse inconsistent totals or trigger definitions; never repair them implicitly."""
    try:
        if (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='logical_usage'"
            ).fetchone()
            is None
        ):
            raise Refused("missing logical accounting table")
        definitions = dict(
            db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'logical_%'"
            )
        )
        if definitions != _triggers():
            raise Refused("logical accounting triggers differ")
        actual = {
            session: _integer(charge)
            for session, charge in db.execute("SELECT session,charge FROM logical_usage")
        }
        if actual != scan(db):
            raise Refused("logical accounting totals differ")
    except sqlite3.DatabaseError as error:
        raise Refused("cannot audit logical accounting") from error


def usage(db: sqlite3.Connection, session: str | None = None) -> int:
    """Read one stored total; a missing known scope is corruption, not free capacity."""
    scope = "" if session is None else _name(session)
    row = db.execute("SELECT charge FROM logical_usage WHERE session=?", (scope,)).fetchone()
    if row is None:
        if scope and db.execute("SELECT 1 FROM sessions WHERE id=?", (scope,)).fetchone() is None:
            return 0
        raise Refused("missing logical total")
    return _integer(row[0], STORE_METADATA if scope == "" else SESSION_METADATA)
