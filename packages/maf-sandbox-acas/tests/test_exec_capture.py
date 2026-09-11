"""Exercise the capture transport with a real POSIX guest and damaged wire replies."""

from __future__ import annotations

import asyncio
import base64
import os
import shlex
import shutil
import sys
from types import SimpleNamespace

import pytest
from maf_sandbox import Egress, ExecResult, SandboxOutputError

from maf_sandbox_acas._backend import _AcasSandbox, _Held
from maf_sandbox_acas._exec_capture import (
    CHUNK_BYTES,
    capture,
    capture_command,
    decode_chunk,
    manifest,
)


@pytest.mark.parametrize(
    "result",
    [
        ExecResult("token 7 2 2\n", exit_code=137),
        ExecResult("token 7 2 2\n", stderr="truncated"),
        ExecResult("token 7 2"),
        ExecResult("token -1 2 2\n"),
        ExecResult("token 256 2 2\n"),
        ExecResult("token 7 11 2\n"),
    ],
)
def test_manifest_refuses_failed_or_over_limit_capture(result):
    with pytest.raises(SandboxOutputError):
        manifest(result, "token", 10)


def test_manifest_requires_the_complete_terminal_newline():
    wire = "token 7 2 2\n"
    assert manifest(ExecResult(wire), "token", 10) == (7, 2, 2)
    with pytest.raises(SandboxOutputError):
        manifest(ExecResult(wire[:-1]), "token", 10)


def test_chunk_requires_the_complete_terminal_newline():
    wire = "token\nAP8=\ntoken\n"
    assert decode_chunk(ExecResult(wire), "token", 2) == b"\x00\xff"
    with pytest.raises(SandboxOutputError):
        decode_chunk(ExecResult(wire[:-1]), "token", 2)


@pytest.mark.parametrize(
    "wire",
    ["token\nAA==", "token\n???\ntoken\n", "token\nAA==\ntoken\n", "x" * (2 * CHUNK_BYTES + 1)],
    ids=["truncated", "invalid", "size", "unbounded"],
)
def test_chunks_refuse_truncation_corruption_size_change_and_unbounded_data(wire):
    with pytest.raises(SandboxOutputError):
        decode_chunk(ExecResult(wire), "token", 2)


class _StalledClient:
    sandbox_id = "test-capture"

    def __init__(self, *, delete_fails=False):
        self.started = asyncio.Event()
        self.deleted = False
        self.delete_fails = delete_fails

    async def exec(self, command, *, working_directory):
        self.started.set()
        await asyncio.Future()

    async def begin_delete(self):
        if self.delete_fails:
            raise OSError("delete unavailable")
        self.deleted = True
        return self

    async def result(self):
        pass


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("delete_fails", [False, True])
def test_timeout_and_cancellation_invalidate_shared_instance_and_attempt_disposal(
    cancel, delete_fails
):
    async def scenario():
        client = _StalledClient(delete_fails=delete_fails)
        held = _Held(client.sandbox_id, egress=(Egress.CLOSED, frozenset()))
        sandbox = _AcasSandbox(client, 1, held=held)
        other = _AcasSandbox(client, 1, held=held)
        task = asyncio.create_task(sandbox.exec("sleep 10", working_directory="/", timeout=0.05))
        await client.started.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError) as raised:
            await task
        assert held.unusable
        assert client.deleted != delete_fails
        if delete_fails:
            assert "disposal must be retried" in raised.value.__notes__[0]
        with pytest.raises(SandboxOutputError, match="invalidated"):
            await other.exec("true", working_directory="/", timeout=1)

    asyncio.run(scenario())


async def _shell(script: str) -> ExecResult:
    proc = await asyncio.create_subprocess_exec(
        "sh", "-c", script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    # Match the service's lossy JSON boundary, rather than giving the adapter a raw shortcut.
    return ExecResult(
        out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), proc.returncode or 0
    )


@pytest.fixture
def local_shell():
    directories = set()

    async def run(script):
        directories.update(
            line[2:] for line in script.splitlines() if line.startswith("d=/tmp/maf-exec-")
        )
        return await _shell(script)

    try:
        yield run
    finally:
        for directory in directories:
            assert directory.startswith("/tmp/maf-exec-") and "/" not in directory[len("/tmp/") :]
            shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX guest; also exercised in live ACAS")
@pytest.mark.parametrize("shell", [False, True])
def test_real_capture_preserves_binary_concurrency_background_writers_and_umask(shell, local_shell):
    async def scenario():
        payload = bytes(range(256)) * 4096
        program = [
            sys.executable,
            "-c",
            "import sys; p=bytes(range(256))*4096; sys.stdout.buffer.write(p); sys.stderr.buffer.write(p[::-1]); sys.exit(7)",
        ]
        command = shlex.join(program) if shell else program
        first, second = await asyncio.gather(
            capture(command, local_shell, len(payload)), capture(command, local_shell, len(payload))
        )
        for result in (first, second):
            assert result.stdout_bytes == payload
            assert result.stderr_bytes == payload[::-1]
            assert result.exit_code == 7
        background = await capture("printf before; (sleep 0.1; printf after) &", local_shell, 100)
        assert background.stdout_bytes == b"beforeafter"
        assert (await capture("umask", local_shell, 100)).stdout == (await _shell("umask")).stdout
        with pytest.raises(SandboxOutputError, match="exceeded"):
            await capture(command, local_shell, 100)

    asyncio.run(scenario())


@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX guest")
def test_guest_capture_refuses_pump_failure_links_and_unwritable_scratch(local_shell, tmp_path):
    async def scenario():
        async def tamper(script):
            result = await local_shell(script)
            if script.startswith("for tool"):
                directory = next(line[2:] for line in script.splitlines() if line.startswith("d="))
                os.unlink(directory + "/stdout")
                os.symlink("/etc/shadow", directory + "/stdout")
            return result

        with pytest.raises(SandboxOutputError):
            await capture("printf secret", tamper, 100)
        helper = tmp_path / "head"
        helper.write_text("#!/bin/sh\nexit 1\n")
        helper.chmod(0o755)

        async def broken(script):
            return await local_shell("PATH=" + shlex.quote(str(tmp_path)) + ":$PATH\n" + script)

        with pytest.raises(SandboxOutputError):
            await asyncio.wait_for(capture("true", broken, 100), 2)
        refused = await _shell(
            capture_command("printf should-not-run", "/proc/maf-exec-readonly", "token", 100)
        )
        assert refused.exit_code != 0 and not refused.stdout

    asyncio.run(scenario())


def test_retrieval_failure_disposes_instead_of_returning_partial_success():
    class Client(_StalledClient):
        async def exec(self, command, *, working_directory):
            if command.startswith("for tool"):
                token = next(
                    line.split("=", 1)[1].removeprefix("/tmp/")
                    for line in command.splitlines()
                    if line.startswith("d=")
                )
                return SimpleNamespace(stdout=f"{token} 7 2 0\n", stderr="", exit_code=0)
            raise OSError("interrupted retrieval")

    async def scenario():
        client = Client()
        with pytest.raises(OSError, match="interrupted"):
            await _AcasSandbox(client, 1).exec("true", working_directory="/", timeout=1)
        assert client.deleted

    asyncio.run(scenario())


def test_chunk_can_preserve_every_byte():
    raw = bytes(range(256))
    wire = "token\n" + base64.b64encode(raw).decode() + "\ntoken\n"
    assert decode_chunk(ExecResult(wire), "token", len(raw)) == raw


def test_capture_and_retrieval_share_one_deadline():
    class Client(_StalledClient):
        async def exec(self, command, *, working_directory):
            if command.startswith("for tool"):
                await asyncio.sleep(0.03)
                token = next(
                    line.split("=", 1)[1].removeprefix("/tmp/")
                    for line in command.splitlines()
                    if line.startswith("d=")
                )
                return SimpleNamespace(stdout=f"{token} 7 2 0\n", stderr="", exit_code=0)
            await asyncio.sleep(0.03)
            raise AssertionError("retrieval received a new deadline")

    async def scenario():
        client = Client()
        with pytest.raises(TimeoutError):
            await _AcasSandbox(client, 1).exec("true", working_directory="/", timeout=0.05)
        assert client.deleted

    asyncio.run(scenario())


def test_a_second_cancellation_waits_for_deletion():
    class Client(_StalledClient):
        async def result(self):
            await asyncio.sleep(0.05)
            self.finished = True

    async def scenario():
        client = Client()
        client.finished = False
        task = asyncio.create_task(
            _AcasSandbox(client, 1).exec("sleep 10", working_directory="/", timeout=60)
        )
        await client.started.wait()
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.finished

    asyncio.run(scenario())
