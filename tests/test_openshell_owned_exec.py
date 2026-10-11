"""Ownership and failure ordering for the experimental OpenShell execution harness."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_path = Path(__file__).resolve().parents[1] / "scripts/experiments/openshell_owned_exec.py"
_spec = importlib.util.spec_from_file_location("openshell_owned_exec", _path)
assert _spec and _spec.loader
probe = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = probe
_spec.loader.exec_module(probe)


class Stream:
    def __init__(self, events=(), *, wait=False, failure=None):
        self.events = events
        self.wait = wait
        self.failure = failure
        self.cancelled = False
        self.started = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        for event in self.events:
            yield event
        if self.failure:
            raise self.failure
        if self.wait:
            await asyncio.Future()

    def cancel(self):
        self.cancelled = True


async def verified():
    return {"verified": True}


def execute(stream, dispose=verified, **kwargs):
    return probe.execute_owned(
        lambda: stream,
        dispose,
        owner={"sandbox_id": "owned-instance", "name": "unique-name"},
        seconds=kwargs.get("seconds", 1),
        output_limit=kwargs.get("output_limit", 7),
        cleanup_seconds=kwargs.get("cleanup_seconds", 1),
    )


def test_binary_streams_and_natural_124_at_exact_combined_cap():
    stream = Stream(
        [
            probe.Event("stdout", b"\x00\xff\xfe\x80"),
            probe.Event("stderr", b"\xfe\x00\xff"),
            probe.Event("exit", exit_code=124),
        ]
    )
    result = asyncio.run(execute(stream))
    assert result.outcome == "exited" and result.exit_code == 124
    assert result.stdout == b"\x00\xff\xfe\x80" and result.stderr == b"\xfe\x00\xff"
    assert result.cleanup_verified and not result.ownership_retained and stream.cancelled


def test_overflow_refuses_offending_chunk_and_waits_for_cleanup():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def cleanup():
            entered.set()
            await release.wait()
            return {"verified": True}

        stream = Stream([probe.Event("stdout", b"1234"), probe.Event("stderr", b"5678")])
        task = asyncio.create_task(execute(stream, cleanup))
        await entered.wait()
        assert not task.done() and stream.cancelled
        release.set()
        result = await task
        assert result.outcome == "output_limit" and result.stdout == b"1234" and not result.stderr
        assert result.cleanup_verified

    asyncio.run(scenario())


def test_fractional_deadline_disposes():
    result = asyncio.run(execute(Stream(wait=True), seconds=0.01))
    assert result.outcome == "deadline" and result.cleanup_verified
    assert result.exit_code is None


def test_repeated_cancellation_cannot_skip_cleanup():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def cleanup():
            entered.set()
            await release.wait()
            return {"verified": True}

        stream = Stream(wait=True)
        task = asyncio.create_task(execute(stream, cleanup))
        await stream.started.wait()
        task.cancel()
        await entered.wait()
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        result = await task
        assert result.outcome == "cancelled" and result.cleanup_verified

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["timeout", "exception", "unverified", "cancelled"])
def test_incomplete_cleanup_retains_identity_and_primary_failure(mode):
    async def cleanup():
        if mode == "timeout":
            await asyncio.Future()
        if mode == "exception":
            raise OSError("observer unavailable")
        if mode == "cancelled":
            raise asyncio.CancelledError
        return {"verified": False}

    result = asyncio.run(
        execute(Stream([probe.Event("stdout", b"12345678")]), cleanup, cleanup_seconds=0.01)
    )
    assert result.outcome == "output_limit"
    assert not result.cleanup_verified and result.ownership_retained
    assert result.owner["sandbox_id"] == "owned-instance"


@pytest.mark.parametrize(
    "events,failure",
    [
        ([], None),
        ([], OSError("disconnected")),
        ([], TimeoutError("transport")),
        ([], asyncio.CancelledError()),
        ([probe.Event("exit", exit_code=0), probe.Event("stdout", b"late")], None),
    ],
)
def test_transport_failure_is_not_guest_success_or_caller_timeout(events, failure):
    result = asyncio.run(execute(Stream(events, failure=failure)))
    assert result.outcome == "transport_error" and result.cleanup_verified


def test_dispatch_failure_still_disposes_owned_sandbox():
    def start():
        raise OSError("dispatch failed")

    result = asyncio.run(
        probe.execute_owned(
            start,
            verified,
            owner={"sandbox_id": "owned-instance"},
            seconds=1,
            output_limit=0,
            cleanup_seconds=1,
        )
    )
    assert result.outcome == "transport_error" and result.cleanup_verified


def test_already_completed_cleanup_task_failure_is_observed(monkeypatch):
    async def scenario():
        def completed_failure(coroutine):
            coroutine.close()
            failed = asyncio.get_running_loop().create_future()
            failed.set_exception(RuntimeError("cleanup task failed"))
            return failed

        monkeypatch.setattr(probe.asyncio, "create_task", completed_failure)
        with pytest.raises(RuntimeError, match="cleanup task failed"):
            await execute(Stream([probe.Event("exit", exit_code=0)]))

    asyncio.run(scenario())
