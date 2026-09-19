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
    a normalized absolute POSIX storage base other than ``/``, honored by the backend and writable
    by Python's ``os.makedirs`` and ``open``. Programs receive ``guest_call_path`` beneath it;
    the working directory is never changed. Without a base, file channels are refused.

    ``use_call_directory=False`` uses the prepared base directly without calling ``makedirs``.
    Exclusive admission and whole-sandbox cleanup still protect successive calls.
    """

    instructions: str
    guest_work_dir: str | None = None
    use_call_directory: bool = True

    def __post_init__(self) -> None:
        if type(self.use_call_directory) is not bool:
            raise ValueError("use_call_directory must be a boolean")
        if not self.use_call_directory and self.guest_work_dir is None:
            raise ValueError("using the storage base directly requires guest_work_dir")
        if not isinstance(cast(object, self.instructions), str) or not self.instructions.strip():
            raise ValueError("runtime instructions must describe the verified Python environment")
        guest_base = self.guest_work_dir
        if guest_base is not None and (
            not isinstance(cast(object, guest_base), str)
            or guest_base == "/"
            or not guest_base.startswith("/")
            or guest_base.startswith("//")
            or "\\" in guest_base
            or "\0" in guest_base
            or posixpath.normpath(guest_base) != guest_base
        ):
            raise ValueError(
                "runtime guest_work_dir must be a normalized absolute POSIX path other than '/'"
            )


def runtime_contract(runtime: CodeactRuntime | None) -> str:
    """An opaque identity for the execution configuration, independent of file-channel wiring."""
    if runtime is None:
        return "codeact:exec:python3"
    encoded = json.dumps(
        (runtime.instructions, runtime.guest_work_dir, runtime.use_call_directory),
        ensure_ascii=True,
    ).encode()
    return "codeact:run_code:" + hashlib.sha256(encoded).hexdigest()


def runtime_program(runtime: CodeactRuntime, code: str, guest_call_path: str) -> str:
    """Prepare the declared file environment without changing Python source semantics."""
    if runtime.guest_work_dir is None:
        return code
    guest_path = (
        posixpath.join(runtime.guest_work_dir, guest_call_path)
        if runtime.use_call_directory
        else runtime.guest_work_dir
    )
    # Compile the user's source separately so future imports and module docstrings still work.
    prepare = (
        f"__import__('os').makedirs({guest_path!r}, exist_ok=True)\n"
        if runtime.use_call_directory
        else ""
    )
    return prepare + f"globals()['guest_call_path'] = {guest_path!r}\nexec({code!r}, globals())\n"
