"""Exercise the research callback bridge without starting a native guest."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import queue
import threading
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_hyperlight_host_tools.py"
_spec = importlib.util.spec_from_file_location("bridge_probe", _SCRIPT)
assert _spec and _spec.loader
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


Policy = probe.Policy


async def assert_revoked(policy):
    async def publish(value):
        pytest.fail("revoked policy reached publication")

    with pytest.raises(RuntimeError, match="closed"):
        await policy.run.call("echo", {"value": 1}, publish=publish)


def callback(run="current", seq=1):
    return {
        "op": "callback",
        "run": run,
        "seq": seq,
        "payload": json.dumps({"name": "echo", "arguments": {"value": 1}}),
    }


def prepared(run="current", seq=1):
    return {"op": "prepared", "run": run, "seq": seq}


def result(**changes):
    return {
        "op": "result",
        "run": "current",
        "seq": 0,
        "stdout": "1",
        "stderr": "",
        "exit_code": 0,
        **changes,
    }


class Reader:
    def __init__(self, messages):
        self.frames = queue.Queue()
        self.reading = threading.Event()
        self.finished = threading.Event()
        for message in messages:
            self.frames.put(probe.encode(message))

    def readline(self, limit):
        self.reading.set()
        value = self.frames.get(timeout=5)
        if not value:
            self.finished.set()
        return value


class Worker(probe.ProbeWorker):
    def __init__(self, messages, *, hold_close=False):
        self._input = io.BytesIO()
        self._output = Reader(messages)
        self.close_started = threading.Event()
        self.close_release = threading.Event()
        if not hold_close:
            self.close_release.set()
        self.closed = False

    def close(self):
        self.close_started.set()
        assert self.close_release.wait(5)
        self.closed = True
        self._output.frames.put(b"")


@pytest.mark.parametrize("seq", [True, 1.0, "1", 0, -1])
def test_callback_sequence_requires_a_positive_integer(seq):
    async def exercise():
        policy = Policy("current")
        try:
            with pytest.raises(ValueError):
                await policy.service(callback(seq=seq))
            assert not policy.events
        finally:
            await policy.cleanup()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "message",
    [
        {"op": "accepted", "run": "current", "seq": 1, "payload": "{}"},
        {"op": "callback", "run": "current", "seq": 1, "payload": []},
        {**callback(), "payload": '{"name":"echo","arguments":{},"name":"payload"}'},
        {**callback(), "payload": "[]"},
        {**callback(), "unexpected": 1},
    ],
)
def test_invalid_callback_messages_do_not_dispatch(message):
    async def exercise():
        policy = Policy("current")
        try:
            with pytest.raises(ValueError):
                await policy.service(message)
            assert not policy.events
        finally:
            await policy.cleanup()

    asyncio.run(exercise())


@pytest.mark.parametrize("message", [prepared(seq=0), prepared(seq=True)])
def test_unsolicited_prepared_marker_is_rejected(message):
    async def exercise():
        policy = Policy("current")
        try:
            with pytest.raises(ValueError):
                await policy.service(message)
            assert not policy.events
        finally:
            await policy.cleanup()

    asyncio.run(exercise())


def test_each_callback_requires_exactly_one_prepared_marker():
    async def exercise():
        policy = Policy("current")
        try:
            await policy.service(callback())
            before = list(policy.events)
            with pytest.raises(ValueError):
                await policy.service(callback(seq=2))
            assert policy.events == before
            await policy.service(prepared())
            before = list(policy.events)
            with pytest.raises(ValueError):
                await policy.service(prepared())
            assert policy.events == before
            await policy.service(callback(seq=2))
            await policy.service(prepared(seq=2))
            assert policy.sequence == 2
        finally:
            await policy.cleanup()

    asyncio.run(exercise())


@pytest.mark.parametrize("message", [callback(), prepared(seq=0)])
def test_completed_policy_refuses_further_worker_messages(message):
    async def exercise():
        policy = Policy("current")
        await policy.cleanup()
        with pytest.raises(RuntimeError, match="closed"):
            await policy.service(message)
        assert not policy.events

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "terminal",
    [
        {"op": "accepted"},
        result(exit_code=True),
        result(stdout=[]),
        {"op": "result", "exit_code": 0},
        {**result(), "extra": 1},
        result(run="old"),
        result(seq=True),
        result(seq=5),
    ],
)
def test_exchange_rejects_invalid_terminal_responses_and_retires(terminal):
    async def exercise():
        policy = Policy("current")
        worker = Worker([terminal])
        try:
            with pytest.raises(ValueError):
                await worker.exchange({"op": "run", "run": "current", "code": "pass"}, policy, 2)
            assert worker.closed
            await assert_revoked(policy)
        finally:
            worker.close_release.set()
            worker.close()
            await policy.cleanup()

    asyncio.run(exercise())


def test_success_revokes_run_authority_without_claiming_native_delivery():
    async def exercise():
        policy = Policy("current")
        worker = Worker([callback(), prepared(), result(seq=1)])
        try:
            actual = await worker.exchange(
                {"op": "run", "run": "current", "code": "pass"}, policy, 2
            )
            assert actual == result(seq=1) and not worker.closed
            await assert_revoked(policy)
            assert not any(e.get("outcome") == "delivered" for e in policy.events)
            with pytest.raises(RuntimeError, match="closed"):
                await policy.service(callback(seq=2))
        finally:
            worker.close()
            await policy.cleanup()
        observations = [e for e in policy.events if e["stage"] == "core_observation"]
        assert observations == [
            {"stage": "core_observation", "outcome": "delivery_uncertain", "bytes": 0}
        ]

    asyncio.run(exercise())


def test_deadline_retires_worker_and_revokes_authority():
    async def exercise():
        policy = Policy("current")
        worker = Worker([])
        try:
            with pytest.raises(TimeoutError):
                await worker.exchange({"op": "run", "run": "current", "code": "pass"}, policy, 0.05)
            assert worker.closed
            await assert_revoked(policy)
            assert worker._output.finished.is_set()
        finally:
            worker.close_release.set()
            worker.close()
            await policy.cleanup()

    asyncio.run(exercise())


def test_repeated_cancellation_waits_for_retirement_and_reader_exit():
    async def exercise():
        policy = Policy("current")
        worker = Worker([], hold_close=True)
        task = asyncio.create_task(
            worker.exchange({"op": "run", "run": "current", "code": "pass"}, policy, 3)
        )
        try:
            assert await asyncio.to_thread(worker._output.reading.wait, 2)
            task.cancel()
            assert await asyncio.to_thread(worker.close_started.wait, 2)
            await assert_revoked(policy)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            worker.close_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert worker.closed and worker._output.finished.is_set()
        finally:
            worker.close_release.set()
            await asyncio.gather(task, return_exceptions=True)
            worker.close()
            await policy.cleanup()

    asyncio.run(exercise())


def test_terminal_result_requires_the_callback_prepared_marker():
    async def exercise():
        policy = Policy("current")
        worker = Worker([callback(), result(seq=1)])
        try:
            with pytest.raises(ValueError, match="prepared"):
                await worker.exchange({"op": "run", "run": "current", "code": "pass"}, policy, 2)
            assert worker.closed
            await assert_revoked(policy)
            with pytest.raises(RuntimeError, match="cannot be reused"):
                await worker.exchange({"op": "init"}, Policy("next"), 2)
        finally:
            worker.close()
            await policy.cleanup()

    asyncio.run(exercise())


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_invalid_deadline_does_not_write_to_worker(timeout):
    async def exercise():
        policy = Policy("current")
        worker = Worker([])
        try:
            with pytest.raises(ValueError, match="timeout"):
                await worker.exchange(
                    {"op": "run", "run": "current", "code": "pass"}, policy, timeout
                )
            assert worker._input.getvalue() == b""
        finally:
            worker.close()
            await policy.cleanup()

    asyncio.run(exercise())


def test_program_deadline_drains_a_stubborn_callback_after_retirement():
    async def exercise():
        policy = Policy("current")
        policy.timeout = 10
        message = callback()
        message["payload"] = json.dumps({"name": "wait", "arguments": {"stubborn": True}})
        worker = Worker([message])
        try:
            with pytest.raises(TimeoutError):
                await worker.exchange({"op": "run", "run": "current", "code": "pass"}, policy, 0.1)
            assert worker.closed and policy.stopped.is_set() and not policy.pending
            await assert_revoked(policy)
            assert not any(e.get("outcome") == "delivered" for e in policy.events)
        finally:
            worker.close()
            await policy.cleanup()

    asyncio.run(exercise())


def test_callback_during_initialization_never_reaches_policy():
    async def exercise():
        policy = Policy("current")
        worker = Worker([callback()])
        try:
            with pytest.raises(ValueError, match="outside a live run"):
                await worker.exchange({"op": "init"}, policy, 2)
            assert worker.closed and not policy.events
            await assert_revoked(policy)
        finally:
            worker.close()
            await policy.cleanup()

    asyncio.run(exercise())


def test_overlapping_exchange_does_not_interrupt_the_owner():
    async def exercise():
        owner = Policy("current")
        other = Policy("other")
        worker = Worker([])
        task = asyncio.create_task(
            worker.exchange({"op": "run", "run": "current", "code": "pass"}, owner, 3)
        )
        try:
            assert await asyncio.to_thread(worker._output.reading.wait, 2)
            before = worker._input.getvalue()
            with pytest.raises(RuntimeError, match="already active"):
                await worker.exchange({"op": "run", "run": "other", "code": "pass"}, other, 2)
            assert worker._input.getvalue() == before and not worker.closed
            worker._output.frames.put(probe.encode(result()))
            assert await task == result()
            await assert_revoked(owner)
        finally:
            worker.close()
            await asyncio.gather(task, return_exceptions=True)
            await owner.cleanup()
            await other.cleanup()

    asyncio.run(exercise())


@pytest.mark.parametrize("stale", [False, True])
def test_reused_worker_only_accepts_the_new_run(stale):
    async def exercise():
        old = Policy("current")
        new = Policy("next")
        worker = Worker([result()])
        try:
            await worker.exchange({"op": "run", "run": "current", "code": "pass"}, old, 2)
            await assert_revoked(old)
            worker._output.frames.put(probe.encode(callback(run="current" if stale else "next")))
            worker._output.frames.put(probe.encode(prepared(run="next")))
            worker._output.frames.put(probe.encode(result(run="next", seq=1)))
            if stale:
                with pytest.raises(ValueError, match="stale"):
                    await worker.exchange({"op": "run", "run": "next", "code": "pass"}, new, 2)
                assert worker.closed and not new.events
            else:
                assert await worker.exchange(
                    {"op": "run", "run": "next", "code": "pass"}, new, 2
                ) == result(run="next", seq=1)
                assert not worker.closed
            await assert_revoked(new)
        finally:
            worker.close()
            await old.cleanup()
            await new.cleanup()

    asyncio.run(exercise())


def test_duplicate_wire_fields_are_refused():
    with pytest.raises(ValueError, match="duplicate"):
        probe.decode(b'{"op":"result","op":"callback"}\n')


@pytest.mark.parametrize("number", ["1e999", "-1e999"])
def test_exponent_overflow_is_rejected_in_wire_data(number):
    with pytest.raises(ValueError, match="finite"):
        probe.decode(('{"value":' + number + "}\n").encode())


@pytest.mark.parametrize("number", ["1e999", "-1e999"])
def test_exponent_overflow_is_rejected_before_callback_dispatch(number):
    async def exercise():
        policy = Policy("current")
        message = callback()
        message["payload"] = '{"name":"echo","arguments":{"value":[' + number + "]}}"
        try:
            with pytest.raises(ValueError, match="finite"):
                await policy.service(message)
            assert not policy.events and not policy.pending
            assert policy.sequence == 0
        finally:
            await policy.cleanup()

    asyncio.run(exercise())


@pytest.mark.parametrize("number", ["1.25", "1e308"])
def test_finite_json_floats_still_reach_the_callback(number):
    async def exercise():
        policy = Policy("current")
        message = callback()
        message["payload"] = '{"name":"echo","arguments":{"value":' + number + "}}"
        try:
            assert probe.decode(('{"value":' + number + "}\n").encode()) == {"value": float(number)}
            response = await policy.service(message)
            assert json.loads(response["response"]) == {"value": float(number)}
        finally:
            await policy.cleanup()

    asyncio.run(exercise())


def test_close_failure_drains_policy_and_retains_the_unfinished_reader():
    async def exercise():
        policy = Policy("current")
        await policy.service(callback())
        failure = OSError("worker retirement failed")

        class BrokenCloseWorker(Worker):
            def close(self):
                self.close_started.set()
                raise failure

        worker = BrokenCloseWorker([])
        try:
            with pytest.raises(OSError) as error:
                await worker.exchange({"op": "run", "run": "current", "code": "pass"}, policy, 0.1)
            assert error.value is failure
            await assert_revoked(policy)
            assert not policy.pending
            assert worker._transfer_task is not None and not worker._transfer_task.done()
            assert not worker.closed
            with pytest.raises(RuntimeError, match="cannot be reused"):
                await worker.exchange({"op": "run", "run": "current", "code": "pass"}, policy, 1)
            assert [e for e in policy.events if e["stage"] == "core_observation"] == [
                {"stage": "core_observation", "outcome": "delivery_uncertain", "bytes": 0}
            ]
        finally:
            Worker.close(worker)
            if worker._transfer_task is not None:
                await asyncio.gather(worker._transfer_task, return_exceptions=True)
            await policy.cleanup()

    asyncio.run(exercise())


def test_retirement_waits_for_transfer_completion_after_close():
    async def exercise():
        policy = Policy("current")
        worker = Worker([])
        reader_release = threading.Event()
        at_eof = threading.Event()
        retirement_returned = asyncio.Event()

        class HeldReader(Reader):
            def readline(self, limit):
                value = super().readline(limit)
                if not value:
                    at_eof.set()
                    assert reader_release.wait(5)
                return value

        worker._output = HeldReader([])

        async def exchange():
            try:
                return await worker.exchange(
                    {"op": "run", "run": "current", "code": "pass"}, policy, 2
                )
            finally:
                retirement_returned.set()

        task = asyncio.create_task(exchange())
        try:
            assert await asyncio.to_thread(worker._output.reading.wait, 2)
            task.cancel()
            assert await asyncio.to_thread(at_eof.wait, 2)
            assert worker.closed
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(retirement_returned.wait(), 0.05)
            reader_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert worker._transfer_task is None
        finally:
            reader_release.set()
            worker.close()
            await asyncio.gather(task, return_exceptions=True)
            await policy.cleanup()

    asyncio.run(exercise())
