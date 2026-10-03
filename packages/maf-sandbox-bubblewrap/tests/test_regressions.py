"""Portable broker and lifecycle failure coverage."""

import asyncio
import base64
import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from maf_sandbox import (
    DeclaredOutput,
    SandboxKey,
    SandboxOutputNotConfined,
    SandboxSpec,
    SandboxTransferCapExceeded,
    TransferLimits,
    collect_outputs,
    make_file_system_sink,
)

from maf_sandbox_bubblewrap import BubblewrapSandboxBackend, BubblewrapSandboxConfig
from maf_sandbox_bubblewrap._backend import FILE_LIMIT, _digest, _identity, _Sandbox


@pytest.mark.parametrize("response", ["broker_cap", "oversized_result", "path_escape"])
def test_collection_classifies_read_refusals(tmp_path: Path, response: str) -> None:
    async def run() -> None:
        process = Mock(returncode=None)
        process.stdin.drain = AsyncMock()
        process.stdout = asyncio.StreamReader()
        process.stdout.feed_data(b'{"id":1,"result":{"kind":"file","size":1}}\n')
        reply = (
            {"result": {"data": base64.b64encode(b"grown").decode()}}
            if response == "oversized_result"
            else {
                "error": "TransferCapExceeded" if response == "broker_cap" else "ValueError",
                "detail": "File exceeds transfer limit"
                if response == "broker_cap"
                else "Path escapes its boundary",
            }
        )
        process.stdout.feed_data(json.dumps({"id": 2, **reply}).encode() + b"\n")
        spec = SandboxSpec(
            kind="transfer",
            work_dir="/maf-sandbox/work",
            declared_outputs=(DeclaredOutput(path="result"),),
            files_out=TransferLimits(max_bytes_per_file=1, max_total_bytes=1, max_files=1),
        )
        diagnostics = asyncio.create_task(asyncio.sleep(0, result=""))
        sandbox = _Sandbox(Mock(), "instance", process, -1, tmp_path / "record", spec, diagnostics)
        try:
            expected = (
                SandboxOutputNotConfined
                if response == "path_escape"
                else SandboxTransferCapExceeded
            )
            with pytest.raises(expected):
                await collect_outputs(
                    sandbox, spec, sink=make_file_system_sink(tmp_path / "landed")
                )
            assert not list((tmp_path / "landed").glob("*"))
        finally:
            await diagnostics

    asyncio.run(run())


def test_write_cap_refuses_before_transport(tmp_path: Path) -> None:
    async def run() -> None:
        process = Mock()
        diagnostics = asyncio.create_task(asyncio.sleep(0, result=""))
        sandbox = _Sandbox(
            Mock(),
            "instance",
            process,
            -1,
            tmp_path / "record",
            SandboxSpec(kind="transfer"),
            diagnostics,
        )
        try:
            with pytest.raises(SandboxTransferCapExceeded):
                await sandbox.write_file("large", bytes(FILE_LIMIT + 1), working_directory=".")
            process.stdin.write.assert_not_called()
        finally:
            await diagnostics

    asyncio.run(run())


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 6])
def test_invalid_exec_timeout_refuses_before_transport(tmp_path: Path, timeout: float) -> None:
    async def run() -> None:
        diagnostics = asyncio.create_task(asyncio.sleep(0, result=""))
        backend = Mock(config=Mock(max_timeout=5, output_bytes=1024))
        sandbox = _Sandbox(
            backend,
            "instance",
            Mock(),
            -1,
            tmp_path / "record",
            SandboxSpec(kind="exec"),
            diagnostics,
        )
        sandbox._request = AsyncMock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
        try:
            with pytest.raises(ValueError, match="timeout"):
                await sandbox.exec(["true"], working_directory=".", timeout=timeout)
            sandbox._request.assert_not_called()
            assert not sandbox.dead
        finally:
            await diagnostics

    asyncio.run(run())


@pytest.mark.parametrize("timeout", [True, False, "1", None, [], object()])
@pytest.mark.parametrize("target", ["configuration", "execution"])
def test_timeout_types_are_rejected(tmp_path: Path, timeout: object, target: str) -> None:
    settings = BubblewrapSandboxConfig(tmp_path, tmp_path, tmp_path)
    if target == "configuration":
        with pytest.raises(ValueError, match="max_timeout"):
            replace(settings, max_timeout=timeout)
        return

    async def run() -> None:
        diagnostics = asyncio.create_task(asyncio.sleep(0, result=""))
        sandbox = _Sandbox(
            Mock(config=settings),
            "instance",
            Mock(),
            -1,
            tmp_path,
            SandboxSpec(kind="exec"),
            diagnostics,
        )
        sandbox._request = AsyncMock()
        try:
            with pytest.raises(ValueError, match="timeout"):
                await sandbox.exec(["true"], working_directory=".", timeout=timeout)  # type: ignore[arg-type]
            sandbox._request.assert_not_called()
        finally:
            await diagnostics

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_queued_exec_does_not_dispatch_or_stop_owner(tmp_path: Path, cancel: bool) -> None:
    async def run() -> None:
        process = Mock(returncode=None)
        diagnostics = asyncio.create_task(asyncio.sleep(0, result=""))
        sandbox = _Sandbox(
            Mock(config=Mock(max_timeout=5, output_bytes=1024)),
            "instance",
            process,
            -1,
            tmp_path,
            SandboxSpec(kind="exec"),
            diagnostics,
        )
        sandbox.stop = AsyncMock()
        await sandbox._serial.acquire()
        queued = asyncio.create_task(sandbox.exec(["true"], working_directory=".", timeout=0.02))
        try:
            await asyncio.sleep(0)
            if cancel:
                queued.cancel()
            done, _ = await asyncio.wait({queued}, timeout=0.3)
            assert queued in done
            with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
                await queued
            process.stdin.write.assert_not_called()
            sandbox.stop.assert_not_called()
            assert sandbox._serial.locked()
        finally:
            queued.cancel()
            await asyncio.gather(queued, return_exceptions=True)
            sandbox._serial.release()
            await diagnostics

    asyncio.run(run())


def test_queued_exec_passes_only_remaining_budget(tmp_path: Path) -> None:
    async def run() -> None:
        process = Mock(returncode=None)
        process.stdin.drain = AsyncMock()
        process.stdout = asyncio.StreamReader()
        process.stdout.feed_data(b'{"id":1,"result":{"stdout":"","stderr":"","exit_code":0}}\n')
        diagnostics = asyncio.create_task(asyncio.sleep(0, result=""))
        sandbox = _Sandbox(
            Mock(config=Mock(max_timeout=5, output_bytes=1024)),
            "instance",
            process,
            -1,
            tmp_path,
            SandboxSpec(kind="exec"),
            diagnostics,
        )
        await sandbox._serial.acquire()
        queued = asyncio.create_task(sandbox.exec(["true"], working_directory=".", timeout=1))
        try:
            await asyncio.sleep(0.05)
            sandbox._serial.release()
            assert (await queued).exit_code == 0
            frame = json.loads(process.stdin.write.call_args.args[0])
            assert 0 < frame["timeout"] < 0.98
            assert not sandbox._serial.locked()
        finally:
            queued.cancel()
            await asyncio.gather(queued, return_exceptions=True)
            await diagnostics

    asyncio.run(run())


@pytest.mark.parametrize("timeout", [0.5, 5])
def test_exec_preserves_accepted_timeout(tmp_path: Path, timeout: float) -> None:
    async def run() -> None:
        diagnostics = asyncio.create_task(asyncio.sleep(0, result=""))
        sandbox = _Sandbox(
            Mock(config=Mock(max_timeout=5, output_bytes=1024)),
            "instance",
            Mock(),
            -1,
            tmp_path / "record",
            SandboxSpec(kind="exec"),
            diagnostics,
        )
        sandbox._request = AsyncMock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
        try:
            assert (
                await sandbox.exec(["true"], working_directory=".", timeout=timeout)
            ).exit_code == 0
            assert sandbox._request.call_args.kwargs["timeout"] == timeout
            assert sandbox._request.call_args.kwargs["transport_timeout"] == timeout
        finally:
            await diagnostics

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["malformed", "busy", "failed"])
def test_disposal_continues_after_record_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    with monkeypatch.context() as platform:
        platform.setattr(sys, "platform", "linux")
        backend = BubblewrapSandboxBackend(BubblewrapSandboxConfig(tmp_path, tmp_path, tmp_path))
    key = SandboxKey("scope", "thread", "agent")
    records: list[Path] = []
    for kind in ("first", "second", "third"):
        identity = _identity(key, kind)
        record = tmp_path / (_digest(identity) + ".json")
        record.write_text(
            json.dumps({"identity": identity, "instance": "a" * 32, "cgroup_root": str(tmp_path)})
        )
        records.append(record)
    if failure == "malformed":
        records[0].write_text("not json")
    monkeypatch.setattr(Path, "glob", lambda self, pattern: iter(records))
    disposed: list[Path] = []

    async def dispose_record(record: Path, instance_id: str | None) -> bool:
        assert instance_id == "a" * 32
        if record == records[0]:
            if failure == "busy":
                raise BlockingIOError("Record owned by another process")
            raise OSError("Failed to remove resource group")
        record.unlink()
        disposed.append(record)
        return True

    monkeypatch.setattr(backend, "_dispose_record", dispose_record)
    result = asyncio.run(backend.dispose(key, instance_id="a" * 32))
    assert result is not None
    assert disposed == records[1:]
    assert records[0].exists()
