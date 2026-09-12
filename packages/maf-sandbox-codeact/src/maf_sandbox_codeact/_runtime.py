"""The host's explicit Python runtime contract for CodeAct."""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass
from typing import cast


@dataclass(frozen=True)
class CodeactRuntime:
    """Select ``run_code`` and state the Python environment the host has verified.

    The host verifies Python statement execution, stdout/stderr results and no expression echo;
    ``instructions`` describes the available facilities. File channels require ``guest_work_dir``:
    an absolute POSIX storage base honored by the backend, writable
    by Python's ``os.makedirs`` and ``open``. Programs receive ``guest_call_path`` beneath it;
    the working directory is never changed. Without a base, file channels are refused.
    """

    instructions: str
    guest_work_dir: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(cast(object, self.instructions), str) or not self.instructions.strip():
            raise ValueError("runtime instructions must describe the verified Python environment")
        guest_base = self.guest_work_dir
        if guest_base is not None and (
            not isinstance(cast(object, guest_base), str)
            or not guest_base.startswith("/")
            or guest_base.startswith("//")
            or "\\" in guest_base
            or "\0" in guest_base
            or posixpath.normpath(guest_base) != guest_base
        ):
            raise ValueError("runtime guest_work_dir must be a normalized absolute POSIX path")


def runtime_contract(runtime: CodeactRuntime | None) -> str:
    """An opaque identity for the execution configuration, independent of file-channel wiring."""
    if runtime is None:
        return "codeact:exec:python3"
    encoded = json.dumps((runtime.instructions, runtime.guest_work_dir), ensure_ascii=True).encode()
    return "codeact:run_code:" + hashlib.sha256(encoded).hexdigest()


def runtime_program(runtime: CodeactRuntime, code: str, guest_call_path: str) -> str:
    """Prepare the declared file environment without changing Python source semantics."""
    if runtime.guest_work_dir is None:
        return code
    guest_path = posixpath.join(runtime.guest_work_dir, guest_call_path)
    # Compile the user's source separately so future imports and module docstrings still work.
    return (
        f"__import__('os').makedirs({guest_path!r}, exist_ok=True)\n"
        f"exec({code!r}, dict(globals(), guest_call_path={guest_path!r}))\n"
    )
