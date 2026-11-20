"""Shared checkpoint limits, validation and local ownership locks."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import BinaryIO

CHUNK = 1024 * 1024
MAX_CHECKPOINT = 2 * 1024**3
MAX_FILES = 128
MAX_RESULT = 2 * CHUNK


class Refused(RuntimeError):
    """An operation cannot preserve session identity or committed state."""


def _name(value: str) -> str:  # pyright: ignore[reportUnusedFunction]
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value):
        raise Refused("invalid call identity")
    return value


def _relative(value: str) -> PurePosixPath:  # pyright: ignore[reportUnusedFunction]
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


def _lock(stream: BinaryIO, acquire: bool) -> None:  # pyright: ignore[reportUnusedFunction]
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN)


def _check_rollback_format(path: Path) -> None:  # pyright: ignore[reportUnusedFunction]
    """Refuse non-rollback SQLite headers without opening or checkpointing their sidecars."""
    try:
        with path.open("rb") as stream:
            header = stream.read(20)
    except FileNotFoundError:
        return
    # SQLite's persisted read/write format versions are at offsets 18 and 19.
    if header[:16] == b"SQLite format 3\x00" and header[18:20] != b"\x01\x01":
        raise Refused("unsupported SQLite journal format")
