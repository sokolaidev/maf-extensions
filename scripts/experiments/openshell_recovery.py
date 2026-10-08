"""Persist confirmed ownership for a single-writer OpenShell recovery experiment."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path


def validate(record: dict) -> None:
    """Reject incomplete or malformed ownership evidence before any recovery mutation."""
    if record.get("format") != 1 or record.get("phase") not in ("owned", "pending", "cleaned"):
        raise ValueError("unsupported ownership record")
    if not re.fullmatch(r"s1797-[0-9a-f]{12}", record.get("name", "")):
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
    record = load(path)
    if record["phase"] == "cleaned":
        return {"cleanup_verified": True, "already_recorded": True, "reobserved": False}
    record["phase"] = "pending"
    save(path, record)
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
        save(path, record)
        result.update(cleanup_verified=True, ownership_retained=False)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result
