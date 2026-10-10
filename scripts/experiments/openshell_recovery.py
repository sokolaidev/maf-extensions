"""Persist local allocation intent and confirmed ownership for OpenShell experiments."""

from __future__ import annotations

import errno
import json
import os
import re
import tempfile
import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path


def validate(record: dict) -> None:
    """Reject incomplete or malformed ownership evidence before any recovery mutation."""
    if record.get("format") != 1 or record.get("phase") not in ("owned", "pending", "cleaned"):
        raise ValueError("unsupported ownership record")
    if not re.fullmatch(r"s1797-[0-9a-f]{12,13}", record.get("name", "")):
        raise ValueError("invalid fixture name")
    for field in ("sandbox_id", "delete_request_id"):
        if str(uuid.UUID(record[field])) != record[field] or uuid.UUID(record[field]).int == 0:
            raise ValueError(f"invalid {field}")
    containers = record.get("containers")
    if not isinstance(containers, list) or len(containers) != 2 or len(set(containers)) != 2:
        raise ValueError("expected two distinct captured containers")
    if not all(isinstance(x, str) and re.fullmatch(r"[0-9a-f]{64}", x) for x in containers):
        raise ValueError("invalid immutable container identity")
    processes = record.get("processes")
    if not isinstance(processes, dict) or not processes:
        raise ValueError("missing process identities")
    if not all(
        re.fullmatch(r"[1-9][0-9]*", pid) and isinstance(start, str) and start.isdecimal()
        for pid, start in processes.items()
    ):
        raise ValueError("invalid process identity")


def save(path: Path, record: dict) -> None:
    """Atomically replace one journal file after syncing its contents; assumes one writer."""
    validate(record)
    _write(path, record)


def _write(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(record, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load(path: Path) -> dict:
    """Load a previously confirmed physical owner, refusing partial evidence."""
    record = json.loads(path.read_text(encoding="utf-8"))
    validate(record)
    return record


def recover(
    path: Path,
    current_id: Callable[[str], str | None],
    delete: Callable[[str, str], str],
    verify_absent: Callable[[dict], bool],
) -> dict:
    """Recover under exclusive name ownership; this lookup is not an atomic delete fence."""
    return _recover(
        load(path), lambda record: save(path, record), current_id, delete, verify_absent
    )


def _recover(record, persist, current_id, delete, verify_absent) -> dict:
    if record["phase"] == "cleaned":
        return {"cleanup_verified": True, "already_recorded": True, "reobserved": False}
    record["phase"] = "pending"
    persist(record)
    result: dict[str, object] = {"cleanup_verified": False, "ownership_retained": True}
    try:
        current = current_id(record["name"])
        result["replacement_seen"] = current is not None and current != record["sandbox_id"]
        if current == record["sandbox_id"]:
            targeted = delete(record["name"], record["delete_request_id"])
            if targeted != record["sandbox_id"]:
                raise RuntimeError("delete response identifies a different physical instance")
        if not verify_absent(record):
            raise RuntimeError("independent observer has not confirmed disposal")
        record["phase"] = "cleaned"
        persist(record)
        result.update(cleanup_verified=True, ownership_retained=False)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


class AuthorityBusyError(RuntimeError):
    """Another local process holds this ledger authority."""


class AllocationAuthority:
    """Serialize one trusted local ledger; all mutation clients must share its lock and history."""

    def __init__(self, root: Path, binding: str, *, initialize: bool = False) -> None:
        self.root = root
        self.binding = binding
        self.pid = os.getpid()
        self.closed = False
        self.operation_lock = threading.Lock()
        if not binding:
            raise ValueError("gateway/workspace binding required")
        if initialize:
            root.mkdir(parents=True, exist_ok=True)
        self.lock = (root / "authority.lock").open("x+b" if initialize else "r+b")
        try:
            if initialize:
                self.lock.write(b"0")
                self.lock.flush()
                os.fsync(self.lock.fileno())
            self.lock.seek(0)
            try:
                if os.name == "nt":
                    locking = import_module("msvcrt")
                    locking.locking(self.lock.fileno(), locking.LK_NBLCK, 1)
                else:
                    locking = import_module("fcntl")
                    locking.flock(self.lock.fileno(), locking.LOCK_EX | locking.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise AuthorityBusyError("allocation authority already held") from exc
                raise
            if initialize:
                if self.path.exists():
                    raise FileExistsError("allocation history already exists")
                _write(
                    self.path,
                    {
                        "format": 2,
                        "binding": binding,
                        "next": 1,
                        "allocations": {},
                    },
                )
            self._read()
        except BaseException:
            self.lock.close()
            self.closed = True
            raise

    @property
    def path(self) -> Path:
        return self.root / "allocations.json"

    def close(self) -> None:
        """Release this process's lock; the lock file must never be removed or replaced."""
        with self._operation():
            self.lock.close()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _read(self) -> dict:
        if self.closed or self.pid != os.getpid():
            raise RuntimeError("allocation authority is closed or inherited by another process")
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if state.get("format") != 2 or state.get("binding") != self.binding:
            raise ValueError("allocation format or gateway/workspace binding mismatch")
        next_number = state["next"]
        rows = state["allocations"]
        if type(next_number) is not int or not 1 <= next_number <= 2**52:
            raise ValueError("invalid allocation counter")
        if not isinstance(rows, dict) or len(rows) != next_number - 1:
            raise ValueError("allocation history is incomplete")
        for number, (name, row) in enumerate(rows.items(), 1):
            if name != f"s1797-{number:013x}":
                raise ValueError("allocation history is out of sequence")
            if row["phase"] not in ("reserved", "uncertain", "owned", "pending", "cleaned"):
                raise ValueError("invalid allocation phase")
            for field in ("create_request_id", "delete_request_id"):
                if str(uuid.UUID(row[field])) != row[field] or not uuid.UUID(row[field]).int:
                    raise ValueError("invalid allocation request identity")
            if not bytes.fromhex(row["request"]):
                raise ValueError("missing saved create request")
            if row["phase"] in ("owned", "pending", "cleaned"):
                owner = row["owner"]
                validate(owner)
                if (
                    owner["name"] != name
                    or owner["phase"] != row["phase"]
                    or owner["delete_request_id"] != row["delete_request_id"]
                ):
                    raise ValueError("allocation ownership mismatch")
        return state

    def allocations(self) -> dict:
        """Return a fresh validated snapshot, including permanent reservation tombstones."""
        return self._read()["allocations"]

    @contextmanager
    def _operation(self):
        if not self.operation_lock.acquire(blocking=False):
            raise RuntimeError("allocation operation already active")
        try:
            yield
        finally:
            self.operation_lock.release()

    def reserve(self, request: Callable[[str, str], bytes]) -> str:
        """Persist the never-reused name and exact request bytes before any network dispatch."""
        with self._operation():
            state = self._read()
            number = state["next"]
            if number == 2**52:
                raise OverflowError("allocation counter exhausted")
            name = f"s1797-{number:013x}"
            request_id = str(uuid.uuid4())
            payload = request(name, request_id)
            if not isinstance(payload, bytes) or not payload:
                raise ValueError("create request must be nonempty bytes")
            state["allocations"][name] = {
                "phase": "reserved",
                "request": payload.hex(),
                "create_request_id": request_id,
                "delete_request_id": str(uuid.uuid4()),
            }
            state["next"] += 1
            _write(self.path, state)
            return name

    def create(self, name: str, send: Callable[[bytes], dict]) -> dict:
        """Dispatch once; lost replies remain uncertain and never authorize name-based adoption."""
        with self._operation():
            state = self._read()
            row = state["allocations"][name]
            if row["phase"] != "reserved":
                raise ValueError("create requires an undispatched reservation")
            row["phase"] = "uncertain"
            _write(self.path, state)
            owner = send(bytes.fromhex(row["request"]))
            if owner["name"] != name:
                raise ValueError("create response identifies another name")
            owner = {**owner, "phase": "owned", "delete_request_id": row["delete_request_id"]}
            validate(owner)
            row.update(phase="owned", owner=owner)
            _write(self.path, state)
            return owner

    def recover(self, name: str, current_id, delete, verify_absent) -> dict:
        """Recover confirmed ownership only; uncertain creates require separate reconciliation."""
        with self._operation():
            state = self._read()
            row = state["allocations"][name]
            if row["phase"] in ("reserved", "uncertain"):
                return {
                    "cleanup_verified": False,
                    "ownership_retained": True,
                    "phase": row["phase"],
                }

            def persist(owner):
                row.update(phase=owner["phase"], owner=owner)
                _write(self.path, state)

            return _recover(row["owner"], persist, current_id, delete, verify_absent)
