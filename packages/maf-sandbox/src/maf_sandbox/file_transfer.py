"""Files in and out of a guest, over the file plane or over the shell, in one refusal vocabulary.

The file plane — ``stat_file``, ``read_file``, ``write_file`` — is confined to the working
directory it is called against. A host that has to reach outside it has ``exec``, which is
unconfined anyway, so a road over the shell widens nothing the guest had not already opened;
it costs the utilities it runs (:data:`SHELL_UTILITIES`), which an ``EXEC`` probe checking
only ``sh`` does not establish. Both roads answer a refusal as one :class:`FileRefusal`, so a
caller maps the guest's shape to its own codes once.

A shell transfer whose command's end is unknown — a timeout, an output overflow, a transport
failure — raises :class:`SandboxShellTransferUnfinished`: the command may still be running and
a write may have landed in part, so the caller treats the instance as unclean. A transfer the
guest refused raises :class:`SandboxFileRefused` and leaves nothing behind.
"""

from __future__ import annotations

import base64
import posixpath
import shlex
import uuid
from enum import StrEnum

from ._outputs import SandboxTransferCapExceeded
from ._protocol import EntryKind, SandboxEntry
from .bounded_exec import BoundedExec, SandboxExecOutputLimitExceeded

__all__ = [
    "SHELL_CHUNK_BYTES",
    "SHELL_UTILITIES",
    "FileRefusal",
    "SandboxFileRefused",
    "SandboxShellTransferFailed",
    "SandboxShellTransferUnfinished",
    "entry_refusal",
    "file_refusal",
    "read_file_over_exec",
    "shell_refusal",
    "write_file_over_exec",
]

#: Bytes of file content per shell command: a command is one argument to ``sh -c``, and 48 KiB
#: of content is 64 KiB of base64, under what every shipped guest accepts.
SHELL_CHUNK_BYTES = 48 * 1024

#: What the shell road runs in the guest. ``test``, ``printf`` and ``echo`` are the shell's own
#: in busybox, dash and bash; these are not.
SHELL_UTILITIES = ("sh", "mkdir", "mv", "base64", "wc")

#: Output budget for a command that answers in a line.
_ANSWER_BYTES = 4096

#: Every command's prefix: the diagnostics this module reads are libc's, and a localised
#: guest would word them otherwise.
_C_LOCALE = "export LC_ALL=C; "


class FileRefusal(StrEnum):
    """Why the guest would not serve a path, on either road."""

    NOT_FOUND = "not_found"
    IS_DIRECTORY = "is_directory"
    PERMISSION_DENIED = "permission_denied"
    INVALID_PATH = "invalid_path"


class SandboxFileRefused(Exception):
    """The guest refused the path; ``refusal`` says why and ``detail`` carries its words."""

    def __init__(self, refusal: FileRefusal, detail: str = "") -> None:
        super().__init__(detail or refusal.value)
        self.refusal = refusal
        self.detail = detail


class SandboxShellTransferUnfinished(RuntimeError):
    """A shell transfer whose command's end is unknown; the instance may be unclean.

    ``over_cap`` is set when what ended it was the read's own output budget: the file outgrew
    the cap mid-read, and the read may still be running.
    """

    def __init__(self, detail: str, *, over_cap: bool = False) -> None:
        super().__init__(detail)
        self.over_cap = over_cap


class SandboxShellTransferFailed(RuntimeError):
    """A shell transfer that finished and failed with no refusal the guest's words name."""


def file_refusal(error: BaseException) -> FileRefusal | None:
    """The refusal a file-plane exception names, or ``None`` when it names none.

    A cap, a timeout and a lost connection are not refusals of the path, and each subclasses
    what a refusal is (``SandboxTransferCapExceeded`` is a ``ValueError``, the other two are
    ``OSError``), so they are answered first; a ``PermissionError`` is an ``OSError`` too, so
    it comes before the branch that folds the rest.
    """
    if isinstance(error, (SandboxTransferCapExceeded, TimeoutError, ConnectionError)):
        return None
    if isinstance(error, PermissionError):
        return FileRefusal.PERMISSION_DENIED
    if isinstance(error, IsADirectoryError):
        return FileRefusal.IS_DIRECTORY
    if isinstance(error, FileNotFoundError):
        return FileRefusal.NOT_FOUND
    if isinstance(error, (NotADirectoryError, ValueError, OSError)):
        # The plane's own refusals: through a link, outside the base, a parent that is a file.
        return FileRefusal.INVALID_PATH
    return None


def entry_refusal(entry: SandboxEntry | None) -> FileRefusal | None:
    """The refusal a stat answers for a read, or ``None`` when the entry is a regular file."""
    if entry is None:
        return FileRefusal.NOT_FOUND
    if entry.kind is EntryKind.DIRECTORY:
        return FileRefusal.IS_DIRECTORY
    if entry.kind is not EntryKind.FILE:
        return FileRefusal.INVALID_PATH
    return None


def shell_refusal(stderr: str) -> FileRefusal | None:
    """The refusal the shell's words name, or ``None`` when they name none.

    The words are libc's under the C locale, which every command here runs under; a missing
    utility or an I/O error names no refusal and is the transfer failing, not the path.
    """
    if "Permission denied" in stderr:
        return FileRefusal.PERMISSION_DENIED
    if "Is a directory" in stderr:
        return FileRefusal.IS_DIRECTORY
    if "Not a directory" in stderr:
        return FileRefusal.INVALID_PATH
    if "o such file" in stderr:
        # "No such file or directory" from libc; "no such file" from busybox's shell.
        return FileRefusal.NOT_FOUND
    return None


async def _run(
    sandbox: BoundedExec,
    command: str,
    *,
    working_directory: str,
    timeout: float,
    budget: int,
    reading: bool = False,
):
    """``exec_bounded`` under the C locale; ``reading`` says the budget is the file's cap."""
    try:
        return await sandbox.exec_bounded(
            _C_LOCALE + command,
            working_directory=working_directory,
            timeout=timeout,
            max_output_bytes=budget,
        )
    except SandboxExecOutputLimitExceeded as overflow:
        raise SandboxShellTransferUnfinished(
            "the command's output passed its budget and the command may still be running",
            over_cap=reading,
        ) from overflow
    except TimeoutError as late:
        raise SandboxShellTransferUnfinished(
            "the command did not finish in time and may still be running"
        ) from late
    except Exception as failed:
        raise SandboxShellTransferUnfinished(
            f"the command's result did not come back: {failed}"
        ) from failed


async def write_file_over_exec(
    sandbox: BoundedExec, path: str, content: bytes, *, working_directory: str, timeout: float
) -> None:
    """Write ``content`` to ``path`` through the shell, whole or not at all.

    Base64 in chunks of :data:`SHELL_CHUNK_BYTES`, into a sibling of the target named for
    this call alone and moved into place once the last chunk landed, so a reader, or a second
    writer over the same path, sees a whole file and never an interleaving of two. Parent
    directories are created. A directory at ``path`` is refused, not entered.

    Raises :class:`SandboxFileRefused`, :class:`SandboxShellTransferUnfinished`,
    :class:`SandboxShellTransferFailed`.
    """
    target = shlex.quote(path)
    parent = shlex.quote(posixpath.dirname(path) or ".")
    staged = shlex.quote(f"{path}.{uuid.uuid4().hex}.part")
    encoded = base64.b64encode(content).decode("ascii")
    step = 4 * (SHELL_CHUNK_BYTES // 3)
    # `--` before every operand a utility takes: a relative path may begin with a dash. A
    # redirection's word is never an option, so `>` and `<` need none.
    commands = [
        f"mkdir -p -- {parent} && if [ -d {target} ]; then echo 'Is a directory' >&2; exit 1; "
        f"fi && : > {staged}"
    ]
    commands += [
        f"printf %s {encoded[start : start + step]} | base64 -d >> {staged}"
        for start in range(0, len(encoded), step)
    ]
    commands.append(f"mv -f -- {staged} {target}")
    for command in commands:
        result = await _run(
            sandbox,
            command,
            working_directory=working_directory,
            timeout=timeout,
            budget=_ANSWER_BYTES,
        )
        if result.exit_code != 0:
            detail = result.stderr.strip()
            refusal = shell_refusal(detail)
            if refusal is None:
                raise SandboxShellTransferFailed(f"the write of {path!r} failed: {detail}")
            raise SandboxFileRefused(refusal, detail)


async def read_file_over_exec(
    sandbox: BoundedExec, path: str, *, working_directory: str, timeout: float, max_bytes: int
) -> bytes:
    """Read the regular file at ``path`` through the shell, refusing anything over ``max_bytes``.

    A probe classifies the path first. ``test -e`` is false for a file behind an unsearchable
    ancestor as for an absent one, so when it is false the probe opens the path and lets the
    shell's own error tell the two apart. The read runs under a budget sized for base64 of
    ``max_bytes``, and what decodes is counted again: a file can grow after the probe.

    Raises :class:`SandboxFileRefused`, ``SandboxTransferCapExceeded``,
    :class:`SandboxShellTransferUnfinished`, :class:`SandboxShellTransferFailed`.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    target = shlex.quote(path)
    probe = (
        f"if [ ! -e {target} ]; then ( : < {target} ) 2>&1; echo missing; "
        f"elif [ -d {target} ]; then echo directory; "
        f"elif [ ! -f {target} ]; then echo other; "
        f"elif [ ! -r {target} ]; then echo unreadable; "
        f"else wc -c < {target}; fi"
    )
    probed = await _run(
        sandbox, probe, working_directory=working_directory, timeout=timeout, budget=_ANSWER_BYTES
    )
    answer = probed.stdout.strip()
    if probed.exit_code != 0 or not answer:
        raise SandboxShellTransferFailed(f"the probe of {path!r} failed: {probed.stderr.strip()}")
    if answer.endswith("missing"):
        detail = answer.removesuffix("missing").strip()
        refusal = shell_refusal(detail) if detail else FileRefusal.NOT_FOUND
        if refusal is None:
            raise SandboxShellTransferFailed(f"the probe of {path!r} could not open it: {detail}")
        raise SandboxFileRefused(refusal, detail)
    if answer == "directory":
        raise SandboxFileRefused(FileRefusal.IS_DIRECTORY)
    if answer == "other":
        raise SandboxFileRefused(FileRefusal.INVALID_PATH)
    if answer == "unreadable":
        raise SandboxFileRefused(FileRefusal.PERMISSION_DENIED)
    if not answer.isdigit():
        raise SandboxShellTransferFailed(f"the probe of {path!r} answered {answer!r}")
    if int(answer) > max_bytes:
        raise SandboxTransferCapExceeded(
            f"{path!r} is {answer} bytes and the caller allowed {max_bytes}"
        )
    # Base64 is 4/3 of the file plus line breaks; the budget bounds a file that grew.
    read = await _run(
        sandbox,
        f"base64 < {target}",
        working_directory=working_directory,
        timeout=timeout,
        budget=max_bytes * 2 + _ANSWER_BYTES,
        reading=True,
    )
    if read.exit_code != 0:
        raise SandboxShellTransferFailed(f"the read of {path!r} failed: {read.stderr.strip()}")
    try:
        content = base64.b64decode("".join(read.stdout.split()), validate=True)
    except ValueError as garbled:
        raise SandboxShellTransferFailed(f"the read of {path!r} returned no base64") from garbled
    if len(content) > max_bytes:
        raise SandboxTransferCapExceeded(
            f"{path!r} came back as {len(content)} bytes and the caller allowed {max_bytes}"
        )
    return content
