"""Conservative remote scratch accounting in the coordinator ownership journal."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from .host_store import Refused, _name

if TYPE_CHECKING:
    from .remote_authority import Authority, Owner

SCHEMA = {
    "remote_scratch_settings": "CREATE TABLE remote_scratch_settings(bytes INTEGER NOT NULL, entries INTEGER NOT NULL, total_bytes INTEGER NOT NULL)",
    "remote_scratch": "CREATE TABLE remote_scratch(token TEXT PRIMARY KEY, session TEXT NOT NULL, call TEXT NOT NULL, owner TEXT NOT NULL, request TEXT NOT NULL, charge INTEGER NOT NULL CHECK(charge>0), entries INTEGER NOT NULL CHECK(entries>0), state TEXT NOT NULL CHECK(state IN ('reserved','cleaning','released')), UNIQUE(session,call))",
}


@dataclass(frozen=True)
class Policy:
    """Fixed per-call scratch bounds and an aggregate allowance across execution hosts."""

    bytes: int
    entries: int
    total_bytes: int

    def __post_init__(self) -> None:
        if any(type(v) is not int or not 0 < v <= 2**63 - 1 for v in asdict(self).values()):
            raise Refused("invalid remote scratch policy")
        if self.bytes > self.total_bytes:
            raise Refused("remote scratch allowance exceeds aggregate budget")


@dataclass(frozen=True)
class Receipt:
    """Immutable verifier input; the token identifies scratch, never a host filesystem path."""

    token: str
    owner: str
    call: str
    request: str
    bytes: int
    entries: int


def receipt(authority: Authority, row: sqlite3.Row | None) -> Receipt:
    """Decode a retained row before exposing it to a trusted verifier."""
    if not isinstance(row, sqlite3.Row):
        raise Refused("unknown remote scratch reservation")
    owner = authority._decode_owner(row["owner"])
    _name(row["call"])
    if (
        owner.session != row["session"]
        or not re.fullmatch(r"[0-9a-f]{32}", row["token"])
        or not re.fullmatch(r"[0-9a-f]{64}", row["request"])
        or authority.scratch_policy is None
        or (row["charge"], row["entries"])
        != (authority.scratch_policy.bytes, authority.scratch_policy.entries)
    ):
        raise Refused("remote scratch binding differs")
    return Receipt(
        row["token"], row["owner"], row["call"], row["request"], row["charge"], row["entries"]
    )


def find(authority: Authority, owner: Owner, call: str, request: bytes) -> Receipt:
    """Read a reservation bound to this exact owner and request, under its guard."""
    row = authority.db.execute(
        "SELECT * FROM remote_scratch WHERE session=? AND call=?", (owner.session, call)
    ).fetchone()
    value = receipt(authority, row)
    if (
        value.owner != authority._encode(owner)
        or value.request != hashlib.sha256(request).hexdigest()
    ):
        raise Refused("remote scratch owner or request differs")
    if row["state"] != "reserved":
        raise Refused("remote scratch is no longer authorized")
    return value


def reserve(authority: Authority, owner: Owner, call: str, request: bytes) -> Receipt:
    """Charge before publication admission; a partial admission remains charged."""
    policy = authority.scratch_policy
    if policy is None:
        raise Refused("remote scratch is not configured")
    _name(call)
    with authority.guard(owner):
        if authority.db.execute(
            "SELECT 1 FROM remote_scratch WHERE session=? AND (call=? OR state!='released')",
            (owner.session, call),
        ).fetchone():
            raise Refused("remote scratch requires explicit reconciliation")
        count, used = authority.db.execute(
            "SELECT count(*),coalesce(sum(CASE WHEN state!='released' THEN charge ELSE 0 END),0) FROM remote_scratch"
        ).fetchone()
        if count >= authority.capacity or used + policy.bytes > policy.total_bytes:
            raise Refused("remote scratch capacity exhausted")
        token = uuid.uuid4().hex
        authority.db.execute(
            "INSERT INTO remote_scratch VALUES(?,?,?,?,?,?,?,'reserved')",
            (
                token,
                owner.session,
                call,
                authority._encode(owner),
                hashlib.sha256(request).hexdigest(),
                policy.bytes,
                policy.entries,
            ),
        )
        return find(authority, owner, call, request)
