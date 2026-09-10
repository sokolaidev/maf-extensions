"""Bounded byte capture before ACAS's text response boundary.

All scratch access runs as the guest, including retrieval and removal. No file-plane
operation grants host authority to a guest-controlled capture path.
"""

from __future__ import annotations

import base64
import binascii
import shlex
from collections.abc import Awaitable, Callable, Sequence
from uuid import uuid4

from maf_sandbox import ExecResult, SandboxOutputError

Run = Callable[[str], Awaitable[ExecResult]]
CHUNK_BYTES = 48 * 1024
TOOLS = "sh mkdir mkfifo head cat wc dd base64 rm rmdir"


def capture_command(command: str | Sequence[str], directory: str, token: str, limit: int) -> str:
    """Drain inherited writers to EOF while retaining at most limit + 1 bytes per stream."""
    cmd = command if isinstance(command, str) else shlex.join(command)
    return f"""for tool in {TOOLS}; do
    command -v "$tool" >/dev/null 2>&1 || exit 127
done
old_umask=$(umask)
umask 077
d={shlex.quote(directory)}
mkdir -m 700 "$d" || exit 125
mkfifo "$d/outpipe" "$d/errpipe" || exit 125
(head -c {limit + 1} > "$d/stdout" || exit 125; cat >/dev/null) < "$d/outpipe" & out_reader=$!
(head -c {limit + 1} > "$d/stderr" || exit 125; cat >/dev/null) < "$d/errpipe" & err_reader=$!
(umask "$old_umask"; exec sh -c {shlex.quote(cmd)}) > "$d/outpipe" 2> "$d/errpipe"
rc=$?
wait "$out_reader" || exit 125
wait "$err_reader" || exit 125
out_size=$(wc -c < "$d/stdout") || exit 125
err_size=$(wc -c < "$d/stderr") || exit 125
printf '%s %s %s %s\\n' {shlex.quote(token)} "$rc" "$out_size" "$err_size"
"""


def manifest(result: ExecResult, token: str, limit: int) -> tuple[int, int, int]:
    """Refuse missing, malformed or over-limit captures before retrieving any bytes."""
    if result.exit_code or result.stderr_bytes or len(result.stdout_bytes) > 200:
        raise SandboxOutputError("ACAS exec capture did not complete cleanly")
    parts = result.stdout.split()
    if (
        len(parts) != 4
        or parts[0] != token
        or not all(p.isascii() and p.isdecimal() for p in parts[1:])
    ):
        raise SandboxOutputError("ACAS exec capture returned an invalid manifest")
    status, out_size, err_size = map(int, parts[1:])
    if status > 255:
        raise SandboxOutputError("ACAS exec capture returned an invalid exit code")
    if max(out_size, err_size) > limit:
        raise SandboxOutputError(f"ACAS exec output exceeded the {limit}-byte per-stream limit")
    return status, out_size, err_size


def decode_chunk(result: ExecResult, token: str, expected: int) -> bytes:
    """Only a complete bounded envelope can contribute bytes to a result."""
    if result.exit_code or result.stderr_bytes or len(result.stdout_bytes) > 2 * CHUNK_BYTES:
        raise SandboxOutputError("ACAS exec capture chunk failed or exceeded its bound")
    lines = result.stdout.splitlines()
    if len(lines) < 2 or lines[0] != token or lines[-1] != token:
        raise SandboxOutputError("ACAS exec capture chunk was truncated")
    try:
        raw = base64.b64decode("".join(lines[1:-1]), validate=True)
    except (ValueError, binascii.Error) as invalid:
        raise SandboxOutputError("ACAS exec capture chunk was not base64") from invalid
    if len(raw) != expected:
        raise SandboxOutputError("ACAS exec capture changed size during retrieval")
    return raw


async def capture(command: str | Sequence[str], run: Run, limit: int) -> ExecResult:
    """Capture, retrieve and remove scratch state under the caller's shared deadline.

    The caller must dispose the sandbox on any abnormal end: remote execution survives
    cancellation of the request, and a failed pump may still have inherited writers.
    """
    token = "maf-exec-" + uuid4().hex
    directory = "/tmp/" + token
    result = await run(capture_command(command, directory, token, limit))
    status, out_size, err_size = manifest(result, token, limit)
    streams: list[bytes] = []
    for name, size in (("stdout", out_size), ("stderr", err_size)):
        chunks: list[bytes] = []
        for offset in range(0, size, CHUNK_BYTES):
            script = f"""test ! -L {shlex.quote(directory)} || exit 125
cd -P {shlex.quote(directory)} || exit 125
test -f {name} && test ! -L {name} || exit 125
printf '%s\\n' {shlex.quote(token)}
dd if={name} bs={CHUNK_BYTES} skip={offset // CHUNK_BYTES} count=1 2>/dev/null | base64
printf '\\n%s\\n' {shlex.quote(token)}
"""
            chunk = await run(script)
            chunks.append(decode_chunk(chunk, token, min(CHUNK_BYTES, size - offset)))
        streams.append(b"".join(chunks))
    cleaned = await run(
        f"test ! -L {shlex.quote(directory)} && cd -P {shlex.quote(directory)} && "
        "rm -f -- stdout stderr outpipe errpipe && "
        f"rmdir -- {shlex.quote(directory)}"
    )
    if cleaned.exit_code or cleaned.stdout_bytes or cleaned.stderr_bytes:
        raise SandboxOutputError("ACAS exec capture scratch cleanup failed")
    return ExecResult(stdout_bytes=streams[0], stderr_bytes=streams[1], exit_code=status)
