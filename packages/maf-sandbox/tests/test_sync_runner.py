"""One loop on a daemon thread serves a synchronous surface from any thread, and after a fork."""

from __future__ import annotations

import asyncio
import os
import threading
import time
import warnings

import pytest

from maf_sandbox import SyncRunner


async def _loop_id() -> int:
    return id(asyncio.get_running_loop())


def test_works_with_no_loop_running():
    runner = SyncRunner()

    async def add(a: int, b: int) -> int:
        return a + b

    assert runner.run(add(1, 2)) == 3


def test_works_from_inside_a_running_loop():
    """A call on a loop's own thread must not trip `asyncio.run`'s nesting refusal."""
    runner = SyncRunner()

    async def scenario() -> int:
        return runner.run(_loop_id())

    assert asyncio.run(scenario()) != 0


def test_an_exception_crosses_back_to_the_caller():
    runner = SyncRunner()

    async def fail() -> None:
        raise ValueError("crossed")

    with pytest.raises(ValueError, match="crossed"):
        runner.run(fail())


def test_one_loop_serves_every_call_from_every_thread():
    runner = SyncRunner(thread_name="sync-under-test")
    seen: list[int] = []
    workers = [
        threading.Thread(target=lambda: seen.append(runner.run(_loop_id()))) for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    seen.append(runner.run(_loop_id()))

    assert len(seen) == len(workers) + 1  # every worker answered, not only the parent
    assert len(set(seen)) == 1
    assert sum(t.name == "sync-under-test" for t in threading.enumerate()) == 1


def test_run_on_the_runner_s_own_loop_thread_is_refused_not_hung():
    """The wait would block the one thread that could run the work; code on that loop awaits."""
    runner = SyncRunner()

    async def nested() -> int:
        return 1

    async def on_the_runners_loop() -> str:
        with pytest.raises(RuntimeError, match="own loop thread") as refused:
            runner.run(nested())
        return str(refused.value)

    assert "await" in runner.submit(on_the_runners_loop()).result(timeout=5)


def test_submit_hands_back_a_future_joinable_from_anywhere():
    runner = SyncRunner()

    async def later() -> str:
        await asyncio.sleep(0.01)
        return "done"

    assert runner.submit(later()).result(timeout=5) == "done"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is POSIX")
def test_a_forked_child_starts_its_own_loop():
    """A fork carries the loop but not its thread; the child must not wait on it forever."""
    runner = SyncRunner()
    assert runner.run(_loop_id()) != 0

    with warnings.catch_warnings():
        # The fork of a multi-threaded process is the scenario, not an accident.
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()  # pyright: ignore[reportAttributeAccessIssue]
    if pid == 0:  # pragma: no cover - the child reports through its exit status
        ok = False
        try:
            ok = runner.run(_loop_id()) != 0
        finally:
            os._exit(0 if ok else 1)
    deadline = time.monotonic() + 30
    while True:
        waited, status = os.waitpid(pid, os.WNOHANG)  # pyright: ignore[reportAttributeAccessIssue]
        if waited == pid:
            break
        if time.monotonic() > deadline:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            pytest.fail("the child never came back from its call")
        time.sleep(0.05)
    assert os.waitstatus_to_exitcode(status) == 0
