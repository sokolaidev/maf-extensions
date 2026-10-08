"""Bounded host-authored file requests for the MXC file-plane experiment."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import asdict, dataclass

from scripts.experiments.mxc_session_patch.host_store import Refused

FORMAT = "mxc-files-request-v1"
MAX_CODE = 65536
MAX_NAME = 512
MAX_FILES = 1024
MAX_BYTES = 64 * 1024 * 1024
_RESERVED = {"con", "prn", "aux", "nul", "conin$", "conout$"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}


def name(value: str) -> str:
    """Require an unambiguous relative file name shared by both host platforms."""
    if not isinstance(value, str):
        raise Refused("file name must be text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise Refused("file name is not UTF-8") from error
    parts = value.split("/")
    if (
        not 0 < len(encoded) <= MAX_NAME
        or len(parts) > 16
        or unicodedata.normalize("NFC", value) != value
        or any(ord(c) < 32 or ord(c) == 127 or c in '\\:*?"<>|' for c in value)
        or any(
            not part
            or part in (".", "..")
            or part.endswith((".", " "))
            or unicodedata.normalize("NFKC", part.split(".", 1)[0]).rstrip(" ").casefold()
            in _RESERVED
            for part in parts
        )
    ):
        raise Refused("file name is not a confined portable relative path")
    return value


def _names(values: tuple[str, ...]) -> None:
    paths = {name(value).casefold() for value in values}
    if len(paths) != len(values):
        raise Refused("duplicate or colliding file names")
    for path in paths:
        parts = path.split("/")
        if any("/".join(parts[:index]) in paths for index in range(1, len(parts))):
            raise Refused("file name is also a parent directory")


@dataclass(frozen=True)
class FileLimits:
    """Host-selected transfer bounds below the experiment's fixed safety ceilings."""

    input_files: int
    input_bytes: int
    artifact_files: int
    artifact_bytes: int
    file_bytes: int

    def __post_init__(self) -> None:
        for key, value in asdict(self).items():
            ceiling = MAX_FILES if key.endswith("files") else MAX_BYTES
            if type(value) is not int or not 0 < value <= ceiling:
                raise Refused("file limits must be positive bounded integers")


@dataclass(frozen=True)
class Input:
    """An immutable host upload; the guest receives a writable copy."""

    name: str
    data: bytes
    lifecycle: str = "call"
    replace: bool = False

    def __post_init__(self) -> None:
        name(self.name)
        if type(self.data) is not bytes:
            raise Refused("input data must be an immutable byte snapshot")
        if self.lifecycle not in ("call", "session") or type(self.replace) is not bool:
            raise Refused("invalid input lifecycle or replacement authority")


@dataclass(frozen=True)
class Request:
    """Validate transfers before admission and bind policy to durable retry identity."""

    code: bytes
    inputs: tuple[Input, ...]
    artifacts: tuple[str, ...]
    limits: FileLimits

    def __post_init__(self) -> None:
        if type(self.code) is not bytes or len(self.code) > MAX_CODE:
            raise Refused("code exceeds the experiment's bound")
        try:
            self.code.decode("utf-8")
        except UnicodeError as error:
            raise Refused("code is not UTF-8") from error
        if type(self.inputs) is not tuple or type(self.artifacts) is not tuple:
            raise Refused("request inventories must be immutable")
        if not isinstance(self.limits, FileLimits):
            raise Refused("missing file limits")
        if (
            len(self.inputs) > self.limits.input_files
            or len(self.artifacts) > self.limits.artifact_files
        ):
            raise Refused("file count exceeds allowance")
        if any(not isinstance(item, Input) for item in self.inputs):
            raise Refused("invalid input inventory")
        if (
            any(len(item.data) > self.limits.file_bytes for item in self.inputs)
            or sum(len(item.data) for item in self.inputs) > self.limits.input_bytes
        ):
            raise Refused("input bytes exceed allowance")
        _names(tuple(item.name for item in self.inputs))
        _names(self.artifacts)

    def identity(self) -> bytes:
        """Return canonical request metadata for the store's request digest."""
        value = {
            "format": FORMAT,
            "code_sha256": hashlib.sha256(self.code).hexdigest(),
            "code_bytes": len(self.code),
            "inputs": [
                {
                    "name": item.name,
                    "bytes": len(item.data),
                    "sha256": hashlib.sha256(item.data).hexdigest(),
                    "lifecycle": item.lifecycle,
                    "replace": item.replace,
                }
                for item in sorted(self.inputs, key=lambda item: item.name)
            ],
            "artifacts": sorted(self.artifacts),
            "limits": asdict(self.limits),
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
