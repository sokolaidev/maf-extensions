"""Bounded host snapshots of the native call directory; no guest-controlled host paths."""

from __future__ import annotations

import base64
import json
import posixpath

from maf_sandbox import EntryKind, SandboxEntry, SandboxTransferCapExceeded
from maf_sandbox.paths import confine_resolve_guest_path

from .files.request import FileLimits, Input, name

GUEST_ROOT = "/workspace/calls"


class Files:
    """Stage immutable inputs and expose the helper's verified directory inventory."""

    def __init__(self, limits: FileLimits):
        self.limits = limits
        self.data: dict[str, bytes] = {}
        self.uploads: dict[str, bytes] = {}
        self.dirs: set[str] = {""}
        self.revision = 0

    def resolve(self, path: str, working_directory: str, *, self_allowed: bool = True) -> str:
        base = confine_resolve_guest_path(working_directory, GUEST_ROOT)
        resolved = confine_resolve_guest_path(path, base)
        if not self_allowed and resolved == base:
            raise ValueError("cannot replace or remove the working directory")
        relative = posixpath.relpath(resolved, GUEST_ROOT)
        if relative == ".":
            return ""
        try:
            name(relative)
        except RuntimeError as error:
            raise ValueError(str(error)) from error
        parts = relative.split("/")
        for index in range(1, len(parts)):
            if "/".join(parts[:index]) in self.data:
                raise NotADirectoryError(relative)
        return relative

    def write(self, path: str, content: bytes, working_directory: str) -> None:
        relative = self.resolve(path, working_directory, self_allowed=False)
        if "/" not in relative:
            raise ValueError("MXC uploads require a call directory beneath /workspace/calls")
        if relative in self.dirs:
            raise IsADirectoryError(path)
        parts = relative.split("/")
        candidates = {"/".join(parts[:i]) for i in range(1, len(parts) + 1)}
        existing = {item.casefold(): item for item in (*self.dirs, *self.data)}
        if any(existing.get(item.casefold(), item) != item for item in candidates):
            raise ValueError("file path collides with an existing portable name")
        size = sum(map(len, self.data.values())) - len(self.data.get(relative, b"")) + len(content)
        if (
            len(content) > self.limits.file_bytes
            or size > self.limits.input_bytes
            or len(self.data) + (relative not in self.data) > self.limits.input_files
            or len(self.dirs | candidates) > 1024
        ):
            raise SandboxTransferCapExceeded("staged files exceed MXC transfer limits")
        self.data[relative] = content
        self.uploads[relative] = content
        self.dirs.update(candidates - {relative})
        self.revision += 1

    def stat(self, relative: str) -> SandboxEntry | None:
        path = posixpath.join(GUEST_ROOT, relative)
        if relative in self.data:
            return SandboxEntry(path, EntryKind.FILE, len(self.data[relative]))
        if relative in self.dirs:
            return SandboxEntry(path, EntryKind.DIRECTORY, None)
        return None

    def read(self, path: str, working_directory: str, max_bytes: int) -> bytes:
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        relative = self.resolve(path, working_directory)
        if relative in self.dirs:
            raise IsADirectoryError(path)
        if relative not in self.data:
            raise FileNotFoundError(path)
        value = self.data[relative]
        if len(value) > max_bytes:
            raise SandboxTransferCapExceeded("file exceeds the caller's byte limit")
        return value

    def list(self, path: str, working_directory: str) -> tuple[SandboxEntry, ...]:
        relative = self.resolve(path, working_directory)
        if relative in self.data:
            raise NotADirectoryError(path)
        if relative not in self.dirs:
            raise FileNotFoundError(path)
        children = sorted(
            item
            for item in self.dirs | self.data.keys()
            if item and posixpath.dirname(item) == relative
        )
        return tuple(
            SandboxEntry(
                posixpath.relpath(
                    posixpath.join(GUEST_ROOT, child),
                    confine_resolve_guest_path(working_directory, GUEST_ROOT),
                ),
                entry.kind,
                entry.size_bytes,
            )
            for child in children
            if (entry := self.stat(child)) is not None
        )

    def remove(self, path: str, working_directory: str, recursive: bool) -> None:
        relative = self.resolve(path, working_directory, self_allowed=False)
        if relative in self.dirs and not recursive:
            raise IsADirectoryError(path)
        self.data = {
            k: v for k, v in self.data.items() if k != relative and not k.startswith(relative + "/")
        }
        self.dirs = {k for k in self.dirs if k != relative and not k.startswith(relative + "/")}
        self.uploads = {
            k: v
            for k, v in self.uploads.items()
            if k != relative and not k.startswith(relative + "/")
        }
        self.revision += 1

    def inputs(self, directory: str) -> tuple[Input, ...]:
        prefix = directory + "/"
        return tuple(
            Input(key.removeprefix(prefix), value)
            for key, value in sorted(self.uploads.items())
            if key.startswith(prefix)
        )

    def publish(self, result: bytes) -> None:
        completion = json.loads(result)
        directory = completion["call_directory"]
        data = {
            key: value for key, value in self.data.items() if not key.startswith(directory + "/")
        }
        dirs = {
            key for key in self.dirs if key != directory and not key.startswith(directory + "/")
        }
        dirs.add(directory)
        dirs.update(directory + "/" + item for item in completion["directories"])
        data.update(
            {
                directory + "/" + item["name"]: base64.b64decode(item["base64"], validate=True)
                for item in completion["artifacts"]
            }
        )
        if (
            len(data) > self.limits.input_files + self.limits.artifact_files
            or sum(map(len, data.values())) > self.limits.input_bytes + self.limits.artifact_bytes
            or len(dirs) > 2048
        ):
            raise SandboxTransferCapExceeded(
                "reclaim previous call snapshots before collecting more files"
            )
        self.data, self.dirs = data, dirs
        self.uploads = {
            key: value for key, value in self.uploads.items() if not key.startswith(directory + "/")
        }
        self.revision += 1
